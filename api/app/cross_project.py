"""**跨工程总览**（P3-4）。

## 要解决的痛点

状态与工程解耦之后，每个工程有自己的库（`projects/<key>/battlefield.sqlite3`），
而进程同一时刻只连**一个**库（决策 D1：不热切库）。于是"我所有工程现在怎么样"
这个问题在界面上无解 —— 看板只看得见当前工程，管家也只能答当前工程。

本模块是纯**读**的聚合：对每个已挂载工程开**只读**连接，读出树的规模、花费、
未清的阻断债与最近一条事件，合并成一张表。

## 三条设计取舍

1. **只读，且绝不建库**。用 `sqlite3` 的只读 URI 打开，库不存在就是"没跑过"，
   **不触发** SQLAlchemy 的建表 —— 否则"看一眼总览"会给每个没跑过的工程
   造一个空库，那是这个模块最不该有的副作用。
2. **不编数据**：读不到就 `available: false` + `reason`，字段一律给 `None`
   而不是 `0`。"0 个任务"与"读不到"在界面上必须能分辨。
3. **单工程失败不拖垮整张表**。一个库损坏不该让总览整个 500 ——
   那会把"有一个工程坏了"变成"总览坏了"，用户失去的正是他需要的信息。

## 与 `state_admin.overview` 的分工

那个回答"状态落在哪、占多大"（**磁盘**视角，用于迁移/重置）；
这个回答"每个工程跑到什么程度、花了多少"（**内容**视角，用于决策）。
两者都在总览里，但不要合并：一个是文件系统事实，一个是数据库事实。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .usage import summarize_usages

# 只读打开。`mode=ro` 是**第二道**防线，不是第一道：
#
# * 第一道是 `_read_project` 里的 `db_path.is_file()` 早返回 —— 没有它，
#   任何连接方式都会**创建**一个空库（`sqlite3.connect` 的默认行为）。
# * 第二道才是这里的 `mode=ro`：库文件被并发删除/无权限时给出明确错误，
#   而不是静默建一个空的。
#
# 实测（2026-10-16）：把 `?mode=ro` 去掉，测试全绿 —— 说明真正挡住"建库"的是那道
# `is_file()`。所以两道都要留，且**不能**把 `mode=ro` 当成建库的保险。
#
# `uri=True` 不可省：少了它，`file:...?mode=ro` 会被当成**字面文件名**，
# SQLite 会在当前工作目录造一个 `file:_..._mode=ro` 的垃圾文件，而真库永远读不到
# —— 表现只是"读不到"，看不出是代码坏了。实测少了它 11 条用例红。
_RO_URI = "file:{path}?mode=ro"


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        _RO_URI.format(path=db_path.as_posix()),
        uri=True,
        timeout=2.0,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _one(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = connection.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _read_project(db_path: Path) -> dict[str, Any]:
    """读一个工程的库。**任何异常都转成 `available: false` + 原因**，不往上抛。"""
    fact: dict[str, Any] = {
        "available": False,
        "reason": "",
        "task_total": None,
        "task_by_status": {},
        "roots": [],
        "spend": None,
        "open_blocking_obligations": None,
        "latest_event": None,
        "lead_goal": None,
    }
    if not db_path.is_file():
        fact["reason"] = "还没跑过（没有数据库文件）"
        return fact

    try:
        connection = _connect(db_path)
    except sqlite3.Error as exc:
        fact["reason"] = f"打不开数据库：{exc}"
        return fact

    try:
        if not _has_table(connection, "task_nodes"):
            fact["reason"] = "库里没有 task_nodes 表（可能是别的工具的库，或迁移未完成）"
            return fact

        rows = connection.execute(
            "SELECT status, COUNT(*) AS n FROM task_nodes GROUP BY status"
        ).fetchall()
        by_status = {str(row["status"]): int(row["n"]) for row in rows}
        fact["task_by_status"] = by_status
        fact["task_total"] = sum(by_status.values())

        # 根节点 = 没有父的节点。深度=0 也基本等价，但 `parent_id IS NULL` 是定义。
        root_rows = connection.execute(
            "SELECT id, title, status, updated_at FROM task_nodes "
            "WHERE parent_id IS NULL ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
        fact["roots"] = [
            {
                "id": str(row["id"]),
                "title": str(row["title"] or ""),
                "status": str(row["status"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
            for row in root_rows
        ]
        # "这个工程在干什么"：最新一棵树的目标，比"3 个根节点"有用得多。
        if root_rows:
            fact["lead_goal"] = str(root_rows[0]["title"] or "")

        # 花费：复用 `usage.summarize_usages`，与当前工程看板**同一份规范化逻辑**。
        # 另写一份的话，总览与看板的钱会对不上，而那种不一致最难解释。
        if _has_table(connection, "run_records"):
            payloads: list[Any] = []
            for row in connection.execute(
                "SELECT usage_json FROM run_records WHERE usage_json IS NOT NULL"
            ):
                try:
                    parsed = json.loads(row["usage_json"])
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict) and parsed:
                    payloads.append(parsed)
            fact["spend"] = summarize_usages(payloads) if payloads else None

        # 未清的阻断债：`obligation_index` 是派生索引，读它比逐节点解析
        # `node_meta_json` 便宜几个数量级（千节点树上的差别是秒级 vs 分钟级）。
        # 索引不存在时**不回落**到解析 node_meta：那会让这一个工程拖慢整张表，
        # 而"索引没建"本身就该被如实报出来。
        if _has_table(connection, "obligation_index"):
            fact["open_blocking_obligations"] = int(
                _one(
                    connection,
                    "SELECT COUNT(*) FROM obligation_index WHERE status='open' AND severity='blocking'",
                )
                or 0
            )

        if _has_table(connection, "event_records"):
            row = connection.execute(
                "SELECT type, message, created_at FROM event_records ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                fact["latest_event"] = {
                    "type": str(row["type"] or ""),
                    "message": str(row["message"] or ""),
                    "created_at": str(row["created_at"] or ""),
                }

        fact["available"] = True
        return fact
    except sqlite3.Error as exc:
        fact["reason"] = f"读取失败：{exc}"
        return fact
    finally:
        connection.close()


def _verdict(fact: dict[str, Any]) -> str:
    """一句话结论。**看板要能一眼看出"哪个工程需要我动手"**。

    顺序即优先级：读不到 > 有阻断债 > 在跑 > 空闲。
    "读不到"排最前，是因为它意味着**你不知道那边什么情况**，比知道有债更糟。
    """
    if not fact["available"]:
        return "unknown"
    if (fact["open_blocking_obligations"] or 0) > 0:
        return "blocked"
    by_status = fact["task_by_status"] or {}
    if by_status.get("running") or by_status.get("queued") or by_status.get("planning"):
        return "running"
    return "idle"


def cross_project(settings: Any) -> dict[str, Any]:
    """所有已挂载工程的树状态、花费与待办动作。**只读，不建库。**"""
    from .project_store import current_project, directory_size_bytes, list_projects

    layout = settings.layout
    state_home = Path(layout.state_home)
    active = current_project(state_home=state_home)
    active_key = active.key if active else ""

    projects: list[dict[str, Any]] = []
    for project in list_projects(state_home=state_home):
        fact = _read_project(Path(project.db_path))
        projects.append(
            {
                "key": project.key,
                "root": str(project.root),
                "active": project.key == active_key,
                "root_exists": Path(project.root).is_dir(),
                "db_bytes": project.db_size_bytes(),
                "state_bytes": directory_size_bytes(project.project_dir),
                **fact,
                "verdict": _verdict(fact),
            }
        )

    # 合计只累计**读得到**的部分，并显式给出有多少工程没读到 ——
    # 不然"合计"会被读成"全部"，而少算的钱比不报更危险。
    totals = _totals(projects)

    return {
        "state_home": str(state_home),
        "active_key": active_key,
        "projects": projects,
        "totals": totals,
        "notes": _notes(projects),
    }


def _totals(projects: list[dict[str, Any]]) -> dict[str, Any]:
    """跨工程合计。`totals_incomplete` 表示有工程没读到，合计**偏小**。

    金额**按币种分桶**，与 `usage.summarize_usages` 同一条规矩：
    不做跨币种相加。不同工程用不同币种时给出两个数，而不是一个假的总额。
    """
    read = [item for item in projects if item["available"]]
    tasks = sum(int(item["task_total"] or 0) for item in read)
    obligations = sum(
        int(item["open_blocking_obligations"] or 0)
        for item in read
        if item["open_blocking_obligations"] is not None
    )

    tokens = 0
    by_currency: dict[str, float] = {}
    unlabeled = 0.0
    for item in read:
        spend = item.get("spend") or {}
        tokens += int(spend.get("total_tokens") or 0)
        for code, amount in (spend.get("cost_by_currency") or {}).items():
            if isinstance(amount, (int, float)):
                by_currency[str(code)] = by_currency.get(str(code), 0.0) + float(amount)
        raw_unlabeled = spend.get("cost_unlabeled")
        if isinstance(raw_unlabeled, (int, float)):
            unlabeled += float(raw_unlabeled)

    return {
        "project_count": len(projects),
        "readable_count": len(read),
        "task_total": tasks,
        "open_blocking_obligations": obligations,
        "total_tokens": tokens,
        # 钱取不到就**不给 0**：0 会被读成"没花钱"，而真相可能是"供应商没报单价"。
        "cost_by_currency": (
            {code: round(value, 6) for code, value in sorted(by_currency.items())}
            if by_currency
            else None
        ),
        # 没有币种标签的金额单列 —— 混进任何一个币种桶都是编数据。
        "cost_unlabeled": round(unlabeled, 6) if unlabeled else None,
        "blocked_projects": [item["key"] for item in read if item["verdict"] == "blocked"],
        "unreadable_projects": [item["key"] for item in projects if not item["available"]],
        "totals_incomplete": len(read) != len(projects),
    }


def _notes(projects: list[dict[str, Any]]) -> list[str]:
    """给界面用的提醒。只写**需要人处理**的事，不写"一切正常"。"""
    notes: list[str] = []
    unreadable = [item["key"] for item in projects if not item["available"]]
    if unreadable:
        notes.append(
            "这些工程的状态读不到，合计里**不含**它们："
            + "、".join(unreadable)
            + "。展开对应行看原因。"
        )
    blocked = [item["key"] for item in projects if item["verdict"] == "blocked"]
    if blocked:
        notes.append("这些工程有未清的阻断债，在等人处理：" + "、".join(blocked) + "。")
    offline = [item["key"] for item in projects if not item["root_exists"]]
    if offline:
        notes.append(
            "这些工程的目录已经不存在（状态还在，但跑不起来）：" + "、".join(offline) + "。"
        )
    return notes
