"""债务的派生索引：把 `TaskNode.node_meta["obligations"]` 摊平成 `obligation_index` 表。

**为什么要有它**（2026-09-22 决策，`docs/spec-parent-child-obligations.md` §13 Q3）：债务的真相
是 `node_meta` 里那份 JSON（门禁读的就是它），但 JSON 里**查不动**——"所有树里 `constraint_coverage_gap`
类型、还开着的债有多少条"这种事只能全表扫 + 反序列化。看板要做跨树聚合就得摊平。

**边界（必须记住，判据的失效方向也在这里）**：
- 本表是**派生缓存，不是第二个真相来源**。真相变了而没重建 → 这张表就是过期的。
  所以：(1) 写入只由本模块的重建入口负责；(2) `check()` 能把"表 vs node_meta"的漂移逐条报出来；
  (3) 读它的地方必须能看到 `built_at`，别把缓存当事实。
- `check()` 与 `rebuild()` **共用同一个投影函数**（`project`）：两处各写一份，漂移判据自己就会漂。
- 读面只此一处：`board()`（经 `GET /api/obligation-index`）。它把"快照是哪一刻"和"还对不对得上"
  与聚合一起给出——只给聚合数会让人把缓存过期的 0 读成"没有债"。重建**不**自动做。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ObligationIndex, TaskNode
from .utils import loads

# 读面能问的三个范围；`all` 映射到 `cross_tree_by_type` 的空串（它那里的约定是"空 = 全都要"）。
STATUS_CHOICES: dict[str, str] = {"open": "open", "settled": "settled", "all": ""}

# 重建命令：索引的唯一写入口是脚本（本模块不提供第二个写入口，见模块 docstring 的边界①）。
REBUILD_COMMAND = "python scripts/rebuild_obligation_index.py --rebuild"

# **不算进漂移判据**的列。`source_updated_at` 是"这行是按哪一刻的节点算出来的"，不是债务内容：
# `orchestrator` 里有 17 处 `task.updated_at = utcnow()`（每次状态流转都动），把它算进判据会让
# 活跃树上的 `in_sync` 近乎**恒假** —— 于是"真漂移"（债务凭空多/少）与"只是节点被动过"都显示成
# "变 N"，读的人分不开，最后两种都被忽略。
# 会不会因此漏掉真漂移？不会：债务内容变了必然改动 `kind` / `severity` / `status` /
# `recurrence_count` / `evidence_version` / `node_id` / `depth` / `root_id` 之一，那些仍然逐列比。
DRIFT_IGNORED_FIELDS = frozenset({"source_updated_at"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _root_map(nodes: list[TaskNode]) -> dict[str, str]:
    """每个节点所属的根 id（沿 `parent_id` 走到顶）。带 `seen` 防脏数据成环。"""
    by_id = {node.id: node for node in nodes}
    roots: dict[str, str] = {}
    for node in nodes:
        cursor = node
        seen: set[str] = set()
        while cursor.parent_id and cursor.id not in seen:
            seen.add(cursor.id)
            parent = by_id.get(cursor.parent_id)
            if parent is None:
                break
            cursor = parent
        roots[node.id] = cursor.id
    return roots


def project(nodes: list[TaskNode]) -> dict[str, dict[str, Any]]:
    """`node_meta["obligations"]` → 行投影（`id -> row`）。**表与漂移检查都走这里**。"""
    roots = _root_map(nodes)
    rows: dict[str, dict[str, Any]] = {}
    for node in nodes:
        obligations = loads(node.node_meta_json, {}).get("obligations")
        if not isinstance(obligations, list):
            continue
        for item in obligations:
            if not isinstance(item, dict):
                continue
            obligation_id = str(item.get("id") or "").strip()
            if not obligation_id:
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            evidence_version = payload.get("baseline_version", payload.get("requirement_version"))
            rows[obligation_id] = {
                "id": obligation_id,
                "root_id": roots.get(node.id),
                "node_id": node.id,
                "depth": node.depth,
                "kind": str(item.get("type") or ""),
                "severity": str(item.get("severity") or ""),
                "status": "settled" if item.get("settled_at") else "open",
                "evidence_version": None if evidence_version is None else str(evidence_version),
                "recurrence_count": int(item.get("recurrence_count") or 0),
                "source_updated_at": node.updated_at,
            }
    return rows


def _scoped_nodes(db: Session, root_id: str | None) -> list[TaskNode]:
    if root_id is None:
        return list(db.scalars(select(TaskNode)))
    all_nodes = list(db.scalars(select(TaskNode)))
    roots = _root_map(all_nodes)
    return [node for node in all_nodes if roots.get(node.id) == root_id]


def rebuild(db: Session, root_id: str | None = None) -> dict[str, Any]:
    """按 `node_meta` 重写索引行（unscoped 时覆盖全库；给了 `root_id` 只重写那棵树）。

    消失的债必须**删掉**：留着的行会被读成"这条债还在"——那是最糟的一种过期（看起来更严格，
    实际是假证据）。
    """
    nodes = _scoped_nodes(db, root_id)
    expected = project(nodes)
    scope_root_ids = {row["root_id"] for row in expected.values()} | ({root_id} if root_id else set())
    existing = {
        row.id: row
        for row in db.scalars(select(ObligationIndex))
        if root_id is None or row.root_id in scope_root_ids
    }
    built_at = _now()
    created = updated = 0
    for obligation_id, row in expected.items():
        current = existing.pop(obligation_id, None)
        if current is None:
            db.add(ObligationIndex(built_at=built_at, **row))
            created += 1
            continue
        changed = False
        for key, value in row.items():
            if getattr(current, key) != value:
                setattr(current, key, value)
                changed = True
        if changed:
            current.built_at = built_at
            updated += 1
    for stale in existing.values():
        db.delete(stale)
    db.flush()
    return {
        "root_id": root_id,
        "nodes_scanned": len(nodes),
        "projected": len(expected),
        "created": created,
        "updated": updated,
        "removed": len(existing),
        "built_at": built_at.isoformat(),
    }


def check(db: Session, root_id: str | None = None) -> dict[str, Any]:
    """把"表 vs node_meta"的漂移逐条报出来（**只读**）。

    失效方向：**报出来但不自动修**——自动重建会把"为什么漂了"这件事一起抹掉，而下一次照样漂。
    """
    nodes = _scoped_nodes(db, root_id)
    expected = project(nodes)
    scope_root_ids = {row["root_id"] for row in expected.values()} | ({root_id} if root_id else set())
    existing = {
        row.id: row
        for row in db.scalars(select(ObligationIndex))
        if root_id is None or row.root_id in scope_root_ids
    }
    missing = sorted(set(expected) - set(existing))
    extra = sorted(set(existing) - set(expected))
    changed: list[str] = []
    for obligation_id in sorted(set(expected) & set(existing)):
        row = existing[obligation_id]
        for key, value in expected[obligation_id].items():
            if key in DRIFT_IGNORED_FIELDS:
                continue
            if getattr(row, key) != value:
                changed.append(f"{obligation_id}:{key}")
    return {
        "root_id": root_id,
        "nodes_scanned": len(nodes),
        "indexed": len(existing),
        "projected": len(expected),
        "in_sync": not (missing or extra or changed),
        "missing": missing,
        "extra": extra,
        "changed": changed,
    }


def cross_tree_by_type(db: Session, *, status: str = "open") -> dict[str, Any]:
    """按类型跨树聚合（本表存在的**唯一理由**）；`status` 取 `open` / `settled` / `""`（全都要）。

    返回里带 `built_at`：读的人必须知道这是哪一刻的快照，否则会把它当实时事实。
    注意这里的 `built_at` 是**本范围那批行**的 max，所以范围为空时它就是 `None` ——
    "范围里没有行"与"索引没建过"在这一层分不开。读面（`board()`）因此**不用**这个值，
    改取全表 max 并配 `projected`，见那边的审计修正说明。
    """
    query = select(ObligationIndex)
    if status:
        query = query.where(ObligationIndex.status == status)
    rows = list(db.scalars(query))
    by_type: dict[str, int] = {}
    by_root: dict[str, int] = {}
    blocking_by_type: dict[str, int] = {}
    for row in rows:
        by_type[row.kind] = by_type.get(row.kind, 0) + 1
        by_root[row.root_id or ""] = by_root.get(row.root_id or "", 0) + 1
        if row.severity == "blocking":
            blocking_by_type[row.kind] = blocking_by_type.get(row.kind, 0) + 1
    return {
        "status": status or "all",
        "total": len(rows),
        "by_type": dict(sorted(by_type.items(), key=lambda item: (-item[1], item[0]))),
        "blocking_by_type": dict(sorted(blocking_by_type.items(), key=lambda item: (-item[1], item[0]))),
        "by_root": by_root,
        "built_at": max((row.built_at for row in rows), default=None),
    }


def board(db: Session, *, status: str = "open") -> dict[str, Any]:
    """读面用的合成：跨树聚合 + **同一时刻**的漂移状态（界面的唯一入口）。

    为什么漂移必须跟聚合一起给：这张表是缓存，"`total == 0`"在缓存过期时**不构成读数**。
    只给聚合会让人把 0 读成"没有债"——那是这一带最贵的一种误读。所以这里返回
    `built_at`（快照是哪一刻，`None` = 表里一行都没有）+ `in_sync` + `drift_counts`（漂在哪一类）
    + `indexed` / `projected`（表里几行 / 真相里几条）。

    **`built_at` 取全表而不是取本范围**（审计修正，2026-09-24）：`cross_tree_by_type` 里的
    `built_at` 是**过滤后那批行**的 max，于是 `total == 0` 恒等于 `built_at is None` ——
    索引明明刚重建过、只是这个范围里确实没有债，也会被读成"索引未建、读不出来"，
    **"确实清干净了"这个状态就此不可表达**（界面上那条"这个范围里没有债务"永远走不到）。
    所以这里改取全表 max，并把 `projected` 一起给出：表空且 `projected == 0` 且 `in_sync`
    才是"真的没有债"，表空但 `projected > 0` 是"索引没建/被清过"。

    失效方向：**如实上报，不自动重建**。自动重建会把"为什么漂了"一起抹掉，下一次照样漂；
    重建是显式动作，命令随读数一起给出（`rebuild_command`）。
    """
    if status not in STATUS_CHOICES:
        raise ValueError(f"status 只能是 {sorted(STATUS_CHOICES)}，收到 {status!r}")
    aggregated = cross_tree_by_type(db, status=STATUS_CHOICES[status])
    drift = check(db)
    return {
        **aggregated,
        "status": status,
        "built_at": db.scalar(select(func.max(ObligationIndex.built_at))),
        "indexed": drift["indexed"],
        "projected": drift["projected"],
        "in_sync": drift["in_sync"],
        "drift_counts": {
            "missing": len(drift["missing"]),
            "extra": len(drift["extra"]),
            "changed": len(drift["changed"]),
        },
        "rebuild_command": REBUILD_COMMAND,
    }
