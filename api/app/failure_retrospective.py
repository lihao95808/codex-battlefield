"""R6 失败复盘：把**失败/阻塞子树**的原始证据收成父层可读的复盘条目。

**为什么需要它**（`docs/spec-resource-governance.md` §R6）：父层 skill note 已经是父主导、
也要求读子树证据，但采集范围**排除失败/阻塞子树**——恰好丢掉最该总结的那部分。成功路径的
经验值钱，失败路径的经验更值钱：它决定了下次要不要换个做法。

本模块只做纯逻辑：从节点**已有的字段**里抽证据 → 按类别聚合 → 组装成一份结构化复盘。
它不碰数据库、不建子任务、不做"要不要修"的判断（那是父层与门控的事）。

口径：只搬运原始证据，**不编造经验条目**——复盘里出现的每一条都能指回某个节点的
`block_report` / `error_message` / `result_json`。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from .models import TaskNode
from .utils import loads, repair_mojibake_text

# `node_meta` 里存复盘的键（由**父层**主导写入）
RETROSPECTIVE_KEY = "failure_retrospective"

# 参与复盘的节点状态
FAILED_STATUSES = ("failed", "blocked")

# 单条证据里保留的理由长度上限：复盘要能读出问题，不是把日志抄一遍
REASON_LIMIT = 400

# 一份复盘里最多带多少条（聚合计数不受此限制）
ENTRY_LIMIT = 20


def _excerpt(value: Any, limit: int = REASON_LIMIT) -> str:
    text = repair_mojibake_text(str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _block_report_of(task: TaskNode, meta: dict[str, Any]) -> dict[str, Any]:
    report = meta.get("block_report")
    return report if isinstance(report, dict) else {}


def _worker_result_of(task: TaskNode) -> dict[str, Any]:
    result = loads(task.result_json or "{}", {})
    if not isinstance(result, dict):
        return {}
    worker = result.get("worker")
    return worker if isinstance(worker, dict) else {}


def entry_from_node(task: TaskNode) -> dict[str, Any] | None:
    """一个节点的失败/阻塞原始证据。不是失败/阻塞 → `None`（成功的节点不进复盘）。

    证据来源按 spec §R6 的四条来：`block_report`（含 `blocked_child_reports`）、
    `error_message`、`result_json`（worker 的失败状态）。
    """
    meta = loads(task.node_meta_json, {})
    if not isinstance(meta, dict):
        meta = {}
    report = _block_report_of(task, meta)
    blocked_children = meta.get("blocked_child_reports")
    blocked_children = blocked_children if isinstance(blocked_children, list) else []
    worker = _worker_result_of(task)
    status = str(task.status or "")
    worker_status = str(worker.get("status") or "")
    own_failure = (
        status in FAILED_STATUSES
        or worker_status in FAILED_STATUSES
        or bool(str(task.error_message or "").strip())
        or bool(report)
    )
    if not own_failure and not blocked_children:
        return None
    # 证据来源必须如实标注：第一版的三选一里没有 `blocked_child_reports`，
    # 于是"父节点名下挂着阻塞子节点"这种最常见的形态被标成 `result_json`，
    # 而按 `source` 过滤时永远查不到这一类（与 README 说的"四条线都收"对不上）。
    if report:
        source = "block_report"
    elif str(task.error_message or "").strip():
        source = "error_message"
    elif blocked_children:
        source = "blocked_child_reports"
    else:
        source = "result_json"
    first_child = blocked_children[0] if blocked_children and isinstance(blocked_children[0], dict) else {}
    rollup = not own_failure and bool(blocked_children)
    reason = report.get("reason") or task.error_message or worker.get("summary") or ""
    if not str(reason or "").strip() and blocked_children:
        reason = first_child.get("reason") or ""
    return {
        "node_id": str(task.id),
        "source_id": str(task.id),
        "title": repair_mojibake_text(str(task.title or "")),
        "depth": task.depth,
        "status": status,
        "worker_status": worker_status,
        # 汇总条目（父节点名下的阻塞子节点）自己没有 category/retryable 可用时，
        # 从第一个被引用的子节点取——写成 "unknown" 会让按类别查询凭空多一个假类别。
        "category": str(report.get("category") or first_child.get("category") or "") or "unknown",
        "retryable": report.get("retryable") if report else first_child.get("retryable"),
        "parent_action": str(report.get("parent_action") or first_child.get("parent_action") or ""),
        "source": source,
        "derived_from_children": rollup,
        "child_failure_ids": [
            str(item.get("task_id") or "")
            for item in blocked_children
            if isinstance(item, dict) and str(item.get("task_id") or "")
        ],
        "reason": _excerpt(reason),
        "blocked_child_count": len(blocked_children),
        "blocked_child_reports": [
            {
                "task_id": str(item.get("task_id") or ""),
                "category": str(item.get("category") or "") or "unknown",
                "retryable": item.get("retryable"),
                "parent_action": str(item.get("parent_action") or ""),
                "reason": _excerpt(item.get("reason"), 200),
            }
            for item in blocked_children[:5]
            if isinstance(item, dict)
        ],
    }


def collect_entries(tasks: Iterable[TaskNode]) -> list[dict[str, Any]]:
    entries = [entry for task in tasks if (entry := entry_from_node(task)) is not None]
    # 浅的先看（越靠近根越该被总结），同层按 id 稳定排序，避免每次跑出来顺序都不一样
    return sorted(entries, key=lambda item: (int(item.get("depth") or 0), str(item.get("node_id") or "")))


def summarize(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """按 spec §R6 的三条线聚合：`category` / `retryable` / `parent_action`。"""
    categories = Counter(str(item.get("category") or "unknown") for item in entries)
    actions = Counter(str(item.get("parent_action") or "") or "(未标注)" for item in entries)
    retryable = Counter(
        "unknown" if item.get("retryable") is None else ("retryable" if item.get("retryable") else "not_retryable")
        for item in entries
    )
    return {
        "total": len(entries),
        "by_category": dict(sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_parent_action": dict(sorted(actions.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_retryable": dict(sorted(retryable.items())),
    }


def build_retrospective(
    *,
    parent_id: str,
    children: Sequence[TaskNode],
    generated_at: str,
    entry_limit: int = ENTRY_LIMIT,
) -> dict[str, Any]:
    """父层复盘的形状（写进 `node_meta[RETROSPECTIVE_KEY]`，也进 `docs/skill_notes` 的产物）。"""
    entries = collect_entries(children)
    return {
        "schema": "codex-battlefield.failure_retrospective.v1",
        "parent_task_id": str(parent_id),
        "generated_at": str(generated_at),
        "summary": summarize(entries),
        "entries": entries[: max(1, entry_limit)],
        "child_ids": sorted(str(task.id) for task in children),
    }


def board(tasks: Sequence[TaskNode], entry_limit: int = ENTRY_LIMIT) -> dict[str, Any]:
    """整棵树的失败面（给看板与"按类别跨树查出"用）：计数 + 有界的最浅若干条明细。

    **去重**：父节点会从 `blocked_child_reports` 派生出自己的一条（"我名下有哪些失败"的汇总），
    而被引用的子节点本来就在同一棵树里、自己也有条目。同一件事算两次会让看板凭空多出一个类别
    （实测 `total` 是真实值的两倍、`by_category` 多一个派生的 `unknown`），
    而"这棵树在哪些类别上失败过"正是这个看板要回答的问题。
    """
    entries = collect_entries(tasks)
    known_ids = {str(item.get("node_id") or "") for item in entries}
    deduped = [
        item
        for item in entries
        if not (item.get("derived_from_children") and known_ids.intersection(item.get("child_failure_ids") or []))
    ]
    return {
        **summarize(deduped),
        "entries": deduped[: max(1, entry_limit)],
    }


def retrospective_block(retrospective: Any) -> str:
    """给提示词用的一段文本：把失败/阻塞证据显式交给父层（skill note 采集范围）。

    没有失败时返回空串——**不要**用"无失败"占位，那会让 prompt 多出一段没信息量的话。
    """
    if not isinstance(retrospective, dict):
        return ""
    entries = retrospective.get("entries")
    if not isinstance(entries, list) or not entries:
        return ""
    summary = retrospective.get("summary") if isinstance(retrospective.get("summary"), dict) else {}
    total = summary.get("total", len(entries))
    listed = len(entries)
    truncation = f"；本次只列出 {listed} 条（其余只在类别计数里）" if isinstance(total, int) and total > listed else ""
    lines = [
        "FAILURE_RETROSPECTIVE（失败/阻塞子树的原始证据，**必须纳入本次复盘**："
        "成功路径的经验值钱，失败路径的经验更值钱）:",
        f"- 条目数：{total}{truncation}；按类别：{summary.get('by_category', {})}；"
        f"可重试分布：{summary.get('by_retryable', {})}",
    ]
    for item in entries:
        lines.append(
            f"- [{item.get('category')}] {item.get('node_id')} ({item.get('status')}) "
            f"source={item.get('source')} parent_action={item.get('parent_action') or '-'} "
            f"retryable={item.get('retryable')}：{item.get('reason')}"
        )
    lines.append(
        "要求：上面列出的条目必须在 skill note 里各有一条经验（同一 category 可合并，但要记录 source_id 与 merged_count；"
        "source_id 就是条目的 `node_id`）；被截断的那部分按类别计数覆盖，不得只读已完成子树。"
    )
    return "\n".join(lines)
