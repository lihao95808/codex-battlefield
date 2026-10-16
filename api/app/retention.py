"""**数据保留**：按时间清理大件，且**不破坏还原能力**（P5-4）。

## 这一层最危险的失效方向

ROADMAP 写得很直白：**"数据清理最容易悄悄破坏恢复能力"**。而且它的表现不是报错 ——
是"某天真的需要恢复时才发现恢复不了"。那一天通常离清理动作已经很久，
所以**清理必须先有还原测试再上线**。

所以本模块的第一条规矩不是"删什么"，而是：

> **先列"不能删什么"**，且这个清单要能从返回值里读出来。

`plan()` 返回 `protected`：每一项都带理由（"它是当前树的还原锚点"/"它不是重建得出来的东西"）。
于是"我没有删掉不该删的"这句话是可核对的，而不是一句承诺。

## 三种东西，三种待遇

| 类别 | 例子 | 待遇 |
|---|---|---|
| **重建得出来** | `runs/` 里的日志与提示词、`codex-home/` | 可以删（有保留窗口） |
| **删了就没了的** | `task_nodes` 等库里的表、宪法、债务 | **永不删** |
| **还原锚点** | 每棵树**最新**的那份快照 | **永不删**（旧的那份才可以） |

"删了就没有"与"重建得出来"的界线是这一层的全部内容。判断标准只有一条：
**删掉之后，还能不能从别处算出同样的东西？**

## 为什么保留窗口必须可配

不同用法的时间尺度差得远：调试一小时 vs 长跑一星期。"合理默认"不存在 ——
所以默认给一个**保守**的值（7 天），并允许改。窗口设为 0 是合法的
（"只留最新的锚点"），但**锚点本身永远不删**。

## 与 worktree 回收的关系

`worktrees.decide()` 已经管了工作树的回收（含它自己的保留期与指纹守卫）。
本模块**不碰工作树** —— 两套清理规则同时作用于同一个目录，
"到底是谁删的"在事故复盘里就查不清了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 默认保留窗口（天）。**保守**：宁可多留，不可少留 —— 少留的那一侧是不可逆的。
DEFAULT_RETENTION_DAYS = 7
# 上限一年。再长就不是"保留策略"而是"永不清理"了，那应该显式关掉这个功能。
MAX_RETENTION_DAYS = 365

# 库里的表**永不删行**。列出来是为了能在界面上回答"为什么这些不清"。
NEVER_TRUNCATE_TABLES: tuple[str, ...] = (
    "task_nodes",
    "constitutions",
    "event_records",
    "steward_messages",
    "steward_actions",
    "goal_deliveries",
    "incident_conclusions",
    "attention_claims",
)

# 大件目录（相对工程状态目录）。`worktrees` **刻意不在**这里（见模块文档）。
SWEEPABLE_DIRS: tuple[str, ...] = (
    "runs",
    "full_checkpoints",
    "checkpoint_ledger",
    "codex-home",
)
# 还原锚点目录。**只删多余的，最先/最后那一份要看清楚**。
ANCHOR_DIR = "state_snapshots"


class RetentionError(ValueError):
    """保留策略不合法。调用方当 4xx 报出去。"""


@dataclass
class Candidate:
    """一个可清理项。`reason` 说明**为什么它可以删**。"""

    path: Path
    kind: str
    bytes: int
    reason: str
    # 它最后被改动的时间（决定它是否过了保留期）
    modified_at: datetime

    def payload(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "bytes": self.bytes,
            "reason": self.reason,
            "modified_at": self.modified_at.isoformat(),
        }


@dataclass
class Protected:
    """一个**不清理**的东西。`reason` 说明为什么它不能删。"""

    path: Path
    reason: str

    def payload(self) -> dict[str, Any]:
        return {"path": str(self.path), "reason": self.reason}


@dataclass
class Plan:
    removable: list[Candidate] = field(default_factory=list)
    protected: list[Protected] = field(default_factory=list)
    # 拿不到时间/读不到的东西 —— **不猜**，列出来让人判断
    unknown: list[str] = field(default_factory=list)

    @property
    def bytes_total(self) -> int:
        return sum(item.bytes for item in self.removable)

    def payload(self) -> dict[str, Any]:
        return {
            "removable": [item.payload() for item in self.removable],
            "protected": [item.payload() for item in self.protected],
            "unknown": list(self.unknown),
            "removable_count": len(self.removable),
            "removable_bytes": self.bytes_total,
            "protected_count": len(self.protected),
        }


def normalize_days(value: Any) -> int:
    """保留窗口（天）。0 合法（"只留锚点"），负数与超过上限都不合法。"""
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise RetentionError(f"保留天数必须是整数，收到 {value!r}。") from exc
    if days < 0:
        raise RetentionError("保留天数不能为负 —— 负窗口没有意义。")
    if days > MAX_RETENTION_DAYS:
        raise RetentionError(
            f"保留天数最多 {MAX_RETENTION_DAYS} 天。再长就不是保留策略而是永不清理 —— "
            "那应该显式关掉清理功能。"
        )
    return days


def _directory_bytes(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _modified(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def anchored_snapshots(
    snapshots_dir: Path, *, now: datetime | None = None
) -> tuple[list[Candidate], list[Protected], list[str]]:
    """快照目录：每棵树**留最新那份**，更早的按保留期清理。

    **锚点本身永不删**：它是"这棵树长什么样"的唯一落盘副本。删了它，
    表被清空时就真的没得还原了 —— 而"表被清空"恰恰是这条通路存在的理由
    （见 `docs/spec-watchdog-steward.md` 里那次快照还原的事故记录）。

    锚点的选取**不依赖时间参数**：它就是"名字最大的那份"（快照文件名以时间戳开头）。
    所以这里不需要 `now`。
    """
    removable: list[Candidate] = []
    protected: list[Protected] = []
    unknown: list[str] = []
    if not snapshots_dir.is_dir():
        return removable, protected, unknown

    for root_dir in sorted(item for item in snapshots_dir.iterdir() if item.is_dir()):
        files = sorted(
            (item for item in root_dir.glob("*.json") if item.is_file()),
            key=lambda item: item.name,
        )
        if not files:
            continue
        anchor = files[-1]
        protected.append(
            Protected(path=anchor, reason="这棵树最新的快照 —— 表被清空时靠它还原，永不清理。")
        )
        for item in files[:-1]:
            stamp = _modified(item)
            if stamp is None:
                unknown.append(f"{item}：读不到修改时间，不敢判它是否过期。")
                continue
            removable.append(
                Candidate(
                    path=item,
                    kind="state_snapshot",
                    bytes=item.stat().st_size if item.is_file() else 0,
                    reason="更早的历史快照（保留了最新那份作为还原锚点）。",
                    modified_at=stamp,
                )
            )
    return removable, protected, unknown


def plan(
    *,
    project_dir: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> Plan:
    """算出可清理项与**受保护项**。**纯计算，不删任何东西。**

    只列大于保留期的项。锚点（每棵树最新的快照）无条件进 `protected` ——
    它不受窗口影响，因为"最新的那份"在任何窗口下都是唯一的。
    """
    days = normalize_days(retention_days)
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=days)
    result = Plan()

    anchors, protected_anchors, unknown_anchors = anchored_snapshots(
        Path(project_dir) / ANCHOR_DIR, now=moment
    )
    result.protected.extend(protected_anchors)
    result.unknown.extend(unknown_anchors)
    for item in anchors:
        if item.modified_at < cutoff:
            result.removable.append(item)

    for name in SWEEPABLE_DIRS:
        target = Path(project_dir) / name
        if not target.is_dir():
            continue
        for child in sorted(target.iterdir()):
            stamp = _modified(child)
            if stamp is None:
                result.unknown.append(f"{child}：读不到修改时间，不敢判它是否过期。")
                continue
            if stamp >= cutoff:
                continue
            result.removable.append(
                Candidate(
                    path=child,
                    kind=name,
                    bytes=_directory_bytes(child) if child.is_dir() else child.stat().st_size,
                    reason=(
                        f"超过 {days} 天没有改动，且属于可重建的内容"
                        "（运行日志/提示词/完整检查点/隔离的 CODEX_HOME）。"
                    ),
                    modified_at=stamp,
                )
            )

    # 库文件本身永不进候选 —— 它是"删了就没有"的那一类。
    result.protected.append(
        Protected(
            path=Path(project_dir) / "battlefield.sqlite3",
            reason="任务树、宪法、事件与债务都在这里，删了就没了；表里的行也永不清理。",
        )
    )
    for name in ("project.json", "resource_leases"):
        path = Path(project_dir) / name
        if path.exists():
            result.protected.append(
                Protected(path=path, reason="工程清单与资源租约 —— 重建不出来，且很小。")
            )
    worktrees = Path(project_dir) / "worktrees"
    if worktrees.is_dir():
        result.protected.append(
            Protected(
                path=worktrees,
                reason=(
                    "工作树由 `worktrees.decide()` 单独管（含它自己的保留期与指纹守卫）。"
                    "两套清理规则作用于同一个目录会让'谁删的'查不清，所以这里不碰。"
                ),
            )
        )

    result.removable.sort(key=lambda item: (item.modified_at, str(item.path)))
    return result


def sweep(plan_result: Plan, *, dry_run: bool = True) -> dict[str, Any]:
    """按计划删除。**默认 `dry_run=True`。**

    为什么默认预演：这个模块的失效方向是"某天需要恢复时才发现恢复不了"，
    而那时离清理已经很远。让"真删"必须显式要一次，是这里唯一便宜的保险。
    """
    removed: list[str] = []
    failed: list[str] = []
    freed = 0
    for item in plan_result.removable:
        if dry_run:
            continue
        try:
            if item.path.is_dir():
                _remove_tree(item.path)
            else:
                item.path.unlink()
            removed.append(str(item.path))
            freed += item.bytes
        except OSError as exc:
            failed.append(f"{item.path}：{exc}")
    return {
        "dry_run": dry_run,
        "planned_count": len(plan_result.removable),
        "planned_bytes": plan_result.bytes_total,
        "removed_count": len(removed),
        "freed_bytes": freed,
        "removed": removed,
        "failed": failed,
        "protected_count": len(plan_result.protected),
        "unknown": list(plan_result.unknown),
        "note": (
            "预演没有删除任何东西。" if dry_run else "已按计划删除；受保护项未被触碰。"
        ),
    }


def _remove_tree(path: Path) -> None:
    """递归删除。**只删自己算出来的候选路径**，不做任何模式匹配。

    刻意不用 `glob`/通配：一个写错一个字符的通配会删掉别的东西，
    而"删错了"在这个模块里是不可逆的。
    """
    import shutil

    shutil.rmtree(path)


def summary(plan_result: Plan, *, retention_days: int) -> str:
    """一行话，给界面与管家用。"""
    return (
        f"保留 {retention_days} 天：可清理 {len(plan_result.removable)} 项"
        f"（{plan_result.bytes_total / 1024:.1f} KB），"
        f"受保护 {len(plan_result.protected)} 项，"
        f"判不了 {len(plan_result.unknown)} 项。"
    )


def tables_report(connection: Any) -> list[dict[str, Any]]:
    """库里的表有多少行 —— 只为**证明它们没有被清理**。

    这一层的判据里有"表行数不变"，所以需要一个能读它的地方；
    而"能读"与"能删"是两件事：本模块没有任何删除行的代码。
    """
    rows: list[dict[str, Any]] = []
    for table in NEVER_TRUNCATE_TABLES:
        try:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - 表名来自本文件的封闭常量
        except Exception:  # noqa: BLE001 - 表不存在（老库）时如实报 0 行
            rows.append({"table": table, "rows": None})
            continue
        rows.append({"table": table, "rows": int(count)})
    return rows
