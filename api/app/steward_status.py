"""管家的**只读状态快照** —— 它的"眼睛"。

## 为什么不让管家自己去探索

管家是有 shell 的 agent，理论上可以自己跑命令看状态。但实测过：API 之外的 shell 里
codex 受限令牌建不起来（`CreateProcessAsUserW failed: 5`），它自己探索**不可靠**。
所以这里把"它该知道的"做成一份结构化快照喂给它 —— 既可靠，又天然把视野限定在
我们愿意给的范围里（权限设计上的好处）。

## 边界（第一版只读）

本模块**只读**：不写文件、不改库、不发任务。管家第一版是看、说、建议，不动手。

## 快照要控制体积

树的节点数可能上千，全量塞进提示词会淹掉重点、也浪费上下文。
所以只给**概况 + 被阻塞的节点（带原因）+ 用量概况 + 最近事件**。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import blockers
from .models import EventRecord, RunRecord, TaskNode, TaskStatus

# 单次快照里最多列几个被阻塞的节点。阻塞原因是最有价值的信号（"为什么卡住了"），
# 但全列会淹没提示词。
MAX_BLOCKED_NODES = 15
MAX_RECENT_EVENTS = 12
# 轮廓最多列几个节点。超出就截断并**明确标注** `outline_truncated`：
# agent 必须知道自己看到的不是全部，否则会做出"树已经完整"的错误判断。
MAX_OUTLINE_NODES = 200
# 用量从最近的 run 记录里聚合。不做全表聚合：那是几千行的 JSON 解析，
# 而管家要的是"大概花了多少"，不是审计数字。
MAX_USAGE_RECORDS = 300


def _status_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(TaskNode.status, func.count()).group_by(TaskNode.status)).all()
    counts = {str(status): int(count) for status, count in rows}
    return {
        "total": sum(counts.values()),
        "done": counts.get(TaskStatus.DONE.value, 0),
        "blocked": counts.get(TaskStatus.BLOCKED.value, 0),
        "failed": counts.get(TaskStatus.FAILED.value, 0),
        "running": counts.get(TaskStatus.RUNNING.value, 0),
        "planned": counts.get(TaskStatus.PLANNED.value, 0),
        "waiting_children": counts.get(TaskStatus.WAITING_CHILDREN.value, 0),
        "paused": counts.get(TaskStatus.PAUSED.value, 0),
        "hibernating": counts.get(TaskStatus.HIBERNATING.value, 0),
    }


def _blocked_nodes(db: Session, settings: Any) -> list[dict[str, Any]]:
    """被阻塞的节点 + **原因**。这是管家最需要知道的东西。

    原因不自己解析 `node_meta_json`，而是复用 `blockers.child_block_report`
    —— 那里面有归一化与缺省逻辑（meta 里没有 block_report 时按 `error_message` 兜底）。
    自己再解析一遍就会成为第二份实现，两边迟早不一致。
    """
    rows = (
        db.execute(
            select(TaskNode)
            .where(TaskNode.status == TaskStatus.BLOCKED.value)
            .order_by(TaskNode.updated_at.desc())
            .limit(MAX_BLOCKED_NODES)
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for node in rows:
        try:
            report = blockers.child_block_report(node, settings)
        except Exception:  # noqa: BLE001 - 快照不许因为单个节点解析失败而整体失败
            report = {}
        out.append(
            {
                "id": node.id,
                "title": node.title,
                "depth": node.depth,
                "category": str(report.get("category") or ""),
                "retryable": bool(report.get("retryable")),
                "reason": str(report.get("reason") or node.error_message or "")[:400],
            }
        )
    return out


def _usage(db: Session) -> dict[str, Any]:
    """用量概况。**取不到就说取不到**，不填 0 —— 0 会被读成"没花钱"。"""
    rows = (
        db.execute(
            select(RunRecord.usage_json)
            .where(RunRecord.usage_json.isnot(None))
            .order_by(RunRecord.id.desc())
            .limit(MAX_USAGE_RECORDS)
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"available": False, "sampled_runs": 0}

    input_tokens = cached = output_tokens = 0
    parsed = 0
    for raw in rows:
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        parsed += 1
        input_tokens += int(payload.get("input_tokens") or 0)
        cached += int(payload.get("cached_input_tokens") or 0)
        output_tokens += int(payload.get("output_tokens") or 0)

    return {
        "available": parsed > 0,
        "sampled_runs": parsed,
        "note": f"只统计最近 {len(rows)} 条有 usage 的记录，不是全量审计数字",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
    }


def _recent_events(db: Session) -> list[dict[str, Any]]:
    rows = (
        db.execute(select(EventRecord).order_by(EventRecord.id.desc()).limit(MAX_RECENT_EVENTS))
        .scalars()
        .all()
    )
    return [
        {
            "type": event.type,
            "message": (event.message or "")[:200],
            "at": event.created_at.isoformat() if event.created_at else None,
        }
        for event in rows
    ]


def _outline(db: Session) -> list[dict[str, Any]]:
    """树的**紧凑轮廓** —— 给 agent 看的，不是给界面看的。

    为什么需要单独一份：`/api/tree` 返回的是完整节点（含 acceptance 列表、结果 JSON、
    scope 等），几百个节点就是几十万字符，塞进提示词既浪费上下文又淹掉重点。
    这里只留"判断局势"需要的字段，并按深度缩进成可读的形状。

    **截断要说出来**：`truncated` 与 `omitted` 是给 agent 的诚实信号 ——
    否则它会以为自己看到的就是全部，进而做出"树已经完整了"的错误判断。
    """
    rows = (
        db.execute(select(TaskNode).order_by(TaskNode.created_at, TaskNode.id))
        .scalars()
        .all()
    )
    if not rows:
        return []

    by_parent: dict[str | None, list[TaskNode]] = {}
    for node in rows:
        by_parent.setdefault(node.parent_id, []).append(node)

    outline: list[dict[str, Any]] = []

    def walk(parent_id: str | None, depth: int) -> None:
        if len(outline) >= MAX_OUTLINE_NODES:
            return
        for node in by_parent.get(parent_id, []):
            if len(outline) >= MAX_OUTLINE_NODES:
                return
            outline.append(
                {
                    "id": node.id,
                    "depth": depth,
                    "status": str(node.status),
                    "title": (node.title or "")[:120],
                    "progress": (
                        f"{node.progress_current}/{node.progress_total}"
                        if node.progress_total
                        else None
                    ),
                }
            )
            walk(node.id, depth + 1)

    walk(None, 0)

    # 有环或孤儿节点时 walk 到不了它们 —— 补上，别让 agent 以为不存在
    seen = {item["id"] for item in outline}
    for node in rows:
        if node.id in seen:
            continue
        if len(outline) >= MAX_OUTLINE_NODES:
            break
        outline.append(
            {
                "id": node.id,
                "depth": node.depth,
                "status": str(node.status),
                "title": (node.title or "")[:120],
                "orphan": True,
            }
        )
    return outline


def _watchdog(settings: Any) -> dict[str, Any]:
    """看门狗 / 资源治理那一路的**只读**视图（`docs/spec-watchdog-steward.md`）。

    为什么管家要看得见看门狗：看门狗是**纯规则**的自动组件，它不会跟用户说话。
    它的结论必须有人转达，否则"机器撑不住了所以我休眠了"对用户不可见 ——
    用户只会看到"树不动了"，然后去翻一堆错误的原因。

    分工不变：**L4 处理"机器撑不撑得住"，管家处理"这事该怎么办"。**
    这里只是把前者的结论读出来，供管家讲给用户听。

    **刻意只读文件，不调 API**：管家跑在 API 进程里，再回头 HTTP 调自己是
    没必要的绕路，还会在 API 忙着的时候自己把自己卡住。
    """
    from .watchdog import watchdog_heartbeat_state

    heartbeat = watchdog_heartbeat_state(settings)
    layout = settings.layout

    guardian: dict[str, Any] = {"available": False}
    guardian_path = layout.machine_path("l4_guardian_state.json")
    try:
        raw = json.loads(guardian_path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            guardian = {
                "available": True,
                "status": raw.get("guardian_status"),
                "mode": raw.get("guardian_mode"),
                "risk_level": raw.get("guardian_risk_level"),
                "summary": raw.get("guardian_summary"),
                "recommended_action": raw.get("guardian_recommended_action"),
                "updated_at": raw.get("guardian_updated_at"),
                "checks": [
                    {"key": c.get("key"), "status": c.get("status"), "detail": c.get("detail")}
                    for c in (raw.get("guardian_checks") or [])
                    if isinstance(c, dict)
                ],
            }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass

    machine: dict[str, Any] = {"available": False}
    machine_path = layout.machine_path("machine_safety.json")
    try:
        raw = json.loads(machine_path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            machine = {
                "available": True,
                "checked_at": raw.get("checked_at"),
                "pressure": raw.get("pressure"),
                "reason": raw.get("reason"),
                "available_memory_mb": raw.get("available_memory_mb"),
            }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass

    hibernating = bool(guardian.get("mode") in {"low_power_sleep", "resource_cooldown"}) or bool(
        machine.get("pressure")
    )
    return {
        "heartbeat": heartbeat,
        "guardian": guardian,
        "machine": machine,
        "hibernating": hibernating,
        "reason": (guardian.get("summary") if hibernating else "") or machine.get("reason") or "",
    }


def build_status(db: Session, settings: Any) -> dict[str, Any]:
    """给管家的只读快照。

    **任何一项取不到都如实标注，不编。** 管家要向用户汇报状态，而"报了个假的 0"
    比"说不知道"坏得多 —— 用户会基于它做决定。
    """
    layout = settings.layout
    outline = _outline(db)
    counts = _status_counts(db)
    return {
        "tool": {
            "state_home": str(layout.state_home),
            "project_key": layout.project_key,
            "project_root": str(layout.project_root) if layout.project_root else None,
            "bound": layout.bound,
            "db_path": str(layout.db_path) if layout.bound else None,
            "runner_mode": settings.runner_mode,
            "frozen_mode": bool(settings.frozen_mode),
        },
        "counts": counts,
        "blocked_nodes": _blocked_nodes(db, settings),
        "usage": _usage(db),
        "recent_events": _recent_events(db),
        "outline": outline,
        "outline_truncated": counts["total"] > len(outline),
        "outline_omitted": max(0, counts["total"] - len(outline)),
        "watchdog": _watchdog(settings),
    }

