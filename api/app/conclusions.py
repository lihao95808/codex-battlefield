"""**结论文档**（P4-5）：把一次事故的来龙去脉整理成可归档记录。

## 要解决的问题

事件表记的是**发生了什么**，它答不了"**当时为什么这么决定**"。而后者恰恰是复盘时
唯一真正需要的东西 —— 事后看到一条 `cancel_node`，光看事件永远不知道那是不是
个合理决定。

## 与 `failure_retrospective` 的分工（容易混，写明）

| | 谁写 | 粒度 | 去哪 |
|---|---|---|---|
| `failure_retrospective` | **代码自动** | 单个失败/阻塞节点 | 上浮给父节点当**提示词材料** |
| 结论文档（本模块） | **人**（管家起草、你改） | 一棵树 / 一次事故 | 归档，供人读 |

前者是喂给模型的，后者是喂给人的。混在一起的后果是"给人看的东西被模型的提示词需求
牵着改"，两边都不好用。

## 三条设计规矩

1. **事实与判断分开**。代码抽出来的叫"事实"（`facts`），人写的那段叫"当时为什么"
   （`authored`）。合并渲染时也分段 —— 混在一起的文档，读者分不清哪句是数据、
   哪句是猜测。
2. **空段 ≠ 没有**。每段都可能是"读不到"或"那类事件压根没发生"。所以草稿必须带
   `incomplete`：列出**这份草稿里没能确定的地方**。把"没有决策点"与"读不到决策点"
   混为一谈，是最坏的一种误读。
3. **只有人写的才算"结论"**。`write()` 要求非空的 `authored` —— 一份只有自动事实、
   没有"当时为什么"的文档不值得归档，它只是另一份事件表。

## 事实从哪来

* `run_records` / 事件表 → 时间线里的**决策点**（本模块不抽"全部事件"，只抽决策：
  宪法锁定/修订、门禁升级、人工清债、休眠与恢复、预算闸、自动并发调整、取消）；
* `task_nodes` → 影响面（状态计数、卡住的节点）；
* 未清阻断债 → "以后什么条件下需要重新打开"；
* 花费（由调用方传 `cost`）→ 代价。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

# ---- 决策点词表 ----
#
# **只抽决策**，不抽全部事件：一份 500 行的时间线没人读，而"当时做了什么决定"
# 通常不超过 20 条。这里按"这个事件是不是一次决定"来筛，不是按严重程度。
DECISION_EVENT_TYPES: tuple[str, ...] = (
    "constitution.locked",
    "constitution.amended",
    "constitution.updated",
    "constitution.acceptance_surface_frozen",
    "constitution.revalidate_exempted",
    "obligation.gate",
    "obligation.settled_human",
    "parent.obligation_gate_blocked",
    "parent.obligation_gate_escalated",
    "parent.human_action_required",
    "task.manual_review_required",
    "task.retry",
    "task.canceled",
    "tree.hibernate",
    "tree.hibernate_resume",
    "budget.guard_hibernate",
    "machine_safety.guard_hibernate",
    "concurrency.auto_adjust",
    "watchdog.external_stall_requeued",
    "scheduler.api_restart_drain",
)

# 每段最多列多少条。文档是给人读的，长了就没人读 —— 超出的只留计数。
TIMELINE_LIMIT = 40
STUCK_NODE_LIMIT = 20

# 终态集合（与 `cost.TERMINAL_STATUSES` 同义；这里不 import 它是为了保持本模块
# 可以独立测试：文档生成不该依赖成本模块）。
TERMINAL_STATUSES = frozenset({"done", "failed", "canceled", "blocked"})


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def decision_timeline(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """从事件里挑出**决策点**，按时序排。

    为什么只挑决策：一份把 300 条事件按时间倒出来的文档没人读，而复盘真正需要的
    信息是"当时在哪些岔路口做了什么选择"。筛选口径是**可解释的**（词表），
    不是按严重程度打分 —— 打分会让"为什么这条在里面"变得说不清。
    """
    picked: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in DECISION_EVENT_TYPES:
            continue
        picked.append(
            {
                "at": str(event.get("created_at") or ""),
                "type": event_type,
                "message": str(event.get("message") or ""),
                "node_id": str(event.get("task_id") or ""),
            }
        )
    picked.sort(key=lambda item: item["at"])
    return picked


def _status_counts(tasks: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _stuck_nodes(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """卡住/失败的节点（最多 `STUCK_NODE_LIMIT` 条）。"""
    stuck = [
        {
            "id": str(task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "status": str(task.get("status") or ""),
            "error": str(task.get("error_message") or "")[:200],
            "attempts": int(task.get("leaf_attempts") or 0),
        }
        for task in tasks
        if str(task.get("status") or "") in {"blocked", "failed"}
    ]
    stuck.sort(key=lambda item: (-item["attempts"], item["id"]))
    return stuck[:STUCK_NODE_LIMIT]


def build_facts(
    *,
    tree: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    open_obligations: Sequence[Mapping[str, Any]] = (),
    cost: Mapping[str, Any] | None = None,
    node_id: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """把一次事故的**事实**抽出来。返回 `(facts, incomplete)`。

    `incomplete` 列的是"这份草稿里没能确定的地方" —— 例如"没有任何决策事件"
    与"事件表里读不到决策"是两件事，而文档读者分不清。所以：

    * 时间线是**空**的 → 写进 `incomplete`（可能是没发生过，也可能是没记上）；
    * 没有花费数据 → 写进 `incomplete`（"没花钱"与"读不到钱"不同）；
    * 没有未清债 → **不写**（"没有欠账"是一个确定的事实，不是缺失）。
    """
    tasks = list(tree.get("tasks") or [])
    scoped = [task for task in tasks if not node_id or str(task.get("id")) == node_id]
    if node_id and not scoped:
        scoped = tasks  # 指定节点不在树里时退回整树，并在 incomplete 里说明

    incomplete: list[str] = []
    if node_id and not any(str(task.get("id")) == node_id for task in tasks):
        incomplete.append(f"指定的节点 {node_id} 不在这棵树里，事实按整棵树抽取。")

    timeline = decision_timeline(events)
    if not timeline:
        incomplete.append(
            "时间线里**没有决策点** —— 要么这段时间确实没有人做过决定（自动流程跑完），"
            "要么决策事件没有被记录。这份文档区分不了这两种情况。"
        )

    counts = _status_counts(scoped)
    total = len(scoped)
    settled = sum(count for status, count in counts.items() if status in TERMINAL_STATUSES)

    facts: dict[str, Any] = {
        "root_id": str(tree.get("root_id") or ""),
        "node_id": node_id,
        "node_count": total,
        "status_counts": counts,
        "settled_count": settled,
        "unsettled_count": total - settled,
        "timeline": timeline[:TIMELINE_LIMIT],
        "timeline_total": len(timeline),
        "stuck_nodes": _stuck_nodes(scoped),
        "open_obligations": [
            {
                "id": str(item.get("id") or item.get("obligation_id") or ""),
                "type": str(item.get("type") or item.get("kind") or ""),
                "node_id": str(item.get("node_id") or item.get("source_node_id") or ""),
                "severity": str(item.get("severity") or ""),
            }
            for item in open_obligations
        ],
    }

    if cost is None:
        incomplete.append(
            "没有花费数据（这次事故的库或用量记录读不到）—— 文档里\"代价\"一段是空的，"
            "不代表没花钱。"
        )
    else:
        facts["cost"] = {
            "total_tokens": int(cost.get("total_tokens") or 0),
            "cost_by_currency": cost.get("cost_by_currency"),
            "cost_unlabeled": cost.get("cost_unlabeled"),
        }

    if total == 0:
        incomplete.append("这棵树没有节点 —— 要么它还没开始，要么读到的是另一棵树。")

    return facts, incomplete


def reopen_conditions(open_obligations: Sequence[Mapping[str, Any]]) -> list[str]:
    """**以后什么条件下需要重新打开**这份结论。

    这一段的实际用途是防止"结案即遗忘"：一份写着"已解决"的文档，
    下次同样的债再出现时没人会想起翻它。把"什么条件下要回来看"写进文档本身。
    """
    conditions: list[str] = []
    for item in open_obligations:
        node = str(item.get("node_id") or item.get("source_node_id") or "?")
        kind = str(item.get("type") or item.get("kind") or "未知类型")
        severity = str(item.get("severity") or "")
        if severity == "blocking":
            conditions.append(f"{node} 上的阻断债 {kind} 仍未清 —— 它一旦再次触发就回来看。")
        else:
            conditions.append(f"{node} 上的 {kind}（advisory）若升级为阻断，回来看。")
    if not conditions:
        conditions.append("当前没有未清债务 —— 只有当同一个症状**再次出现**时才需要回来看。")
    return conditions


def render_markdown(
    *,
    title: str,
    facts: Mapping[str, Any],
    incomplete: Sequence[str],
    authored: str,
    author: str = "",
) -> str:
    """把事实 + 人的判断渲染成一份可归档的 Markdown。

    **事实与判断必须分段**，而且"没能确定的地方"要有自己的一段：
    读者要能一眼看出哪句是代码抽的、哪句是人写的、哪些还没弄清。
    """
    counts = facts.get("status_counts") or {}
    lines: list[str] = [f"# {title or '事故结论'}", ""]

    lines.append("## 影响面（代码抽取）")
    lines.append("")
    lines.append(
        f"- 节点 {facts.get('node_count', 0)} 个，已结算 {facts.get('settled_count', 0)} 个，"
        f"未结算 {facts.get('unsettled_count', 0)} 个"
    )
    if counts:
        lines.append("- 状态分布：" + "、".join(f"{key} {value}" for key, value in counts.items()))
    stuck = facts.get("stuck_nodes") or []
    if stuck:
        lines.append(f"- 卡住/失败的节点（前 {len(stuck)} 个）：")
        for node in stuck:
            suffix = f"（尝试 {node['attempts']} 次）" if node.get("attempts") else ""
            error = f"：{node['error']}" if node.get("error") else ""
            lines.append(f"  - `{node['id']}` {node.get('title', '')} [{node.get('status', '')}]{suffix}{error}")
    else:
        lines.append("- 没有卡住或失败的节点。")
    lines.append("")

    lines.append("## 做了什么决定（代码抽取的决策点）")
    lines.append("")
    timeline = facts.get("timeline") or []
    if timeline:
        for item in timeline:
            node = f"（节点 `{item['node_id']}`）" if item.get("node_id") else ""
            lines.append(f"- `{item.get('at', '')}` **{item.get('type', '')}** {item.get('message', '')}{node}")
        total = int(facts.get("timeline_total") or len(timeline))
        if total > len(timeline):
            lines.append(f"- …… 另有 {total - len(timeline)} 条同类决策点未列出（上限 {TIMELINE_LIMIT}）。")
    else:
        lines.append("- 没有决策点。**这不等于没人做过决定** —— 见下面的「没能确定」。")
    lines.append("")

    lines.append("## 代价（代码抽取）")
    lines.append("")
    cost = facts.get("cost")
    if cost:
        lines.append(f"- token：{cost.get('total_tokens', 0)}")
        by_currency = cost.get("cost_by_currency")
        if by_currency:
            lines.append("- 金额：" + "、".join(f"{code} {value}" for code, value in by_currency.items()))
        else:
            lines.append("- 金额：没有可用的计价数据（**不是 0**）。")
        if cost.get("cost_unlabeled"):
            lines.append(f"- 未标币种金额：{cost['cost_unlabeled']}")
    else:
        lines.append("- 没有花费数据（读不到，**不代表没花钱**）。")
    lines.append("")

    lines.append("## 当时为什么这么决定（**人写**）")
    lines.append("")
    lines.append(authored.strip() if authored.strip() else "- （还没写。一份只有事实的文档不值得归档，它只是另一份事件表。）")
    if author:
        lines.append("")
        lines.append(f"— {author}")
    lines.append("")

    lines.append("## 以后什么条件下需要重新打开")
    lines.append("")
    for condition in reopen_conditions(facts.get("open_obligations") or []):
        lines.append(f"- {condition}")
    lines.append("")

    lines.append("## 这份草稿里没能确定的地方")
    lines.append("")
    if incomplete:
        for item in incomplete:
            lines.append(f"- {item}")
    else:
        lines.append("- （没有未确定项。）")
    lines.append("")

    return "\n".join(lines)


def write(
    db: Any,
    *,
    root_id: str,
    authored: str,
    title: str = "",
    node_id: str = "",
    author: str = "",
    facts: Mapping[str, Any] | None = None,
    incomplete: Sequence[str] = (),
    status: str = "draft",
    conclusion_id: int | None = None,
) -> Any:
    """落一份结论。**`authored` 必填**（见模块文档的第 3 条规矩）。

    没有 `authored` 的结论会被拒：一份只有自动事实的文档不值得归档 ——
    它只是另一份事件表，而且会因为"看起来像结论"而更少被人重新检查。
    """
    import json

    from .models import IncidentConclusion, utcnow

    if not authored.strip():
        raise ValueError(
            "结论必须包含人写的「当时为什么这么决策」一段。"
            "只有自动事实的文档不值得归档 —— 那只是另一份事件表。"
        )
    if status not in {"draft", "final"}:
        raise ValueError(f"未知状态 {status!r}，只能是 draft 或 final。")
    if not root_id.strip():
        raise ValueError("结论必须指明它属于哪棵树（root_id）。")

    payload = dict(facts or {})
    markdown = render_markdown(
        title=title or f"结论 {root_id}",
        facts=payload,
        incomplete=list(incomplete),
        authored=authored,
        author=author,
    )

    if conclusion_id is not None:
        row = db.get(IncidentConclusion, conclusion_id)
        if row is None:
            raise ValueError(f"找不到结论 {conclusion_id}。")
        row.title = title or row.title
        row.authored = authored
        row.markdown = markdown
        row.status = status
        row.author = author or row.author
        row.updated_at = utcnow()
        if payload:
            row.facts_json = json.dumps(payload, ensure_ascii=False, default=str)
            row.incomplete_json = json.dumps(list(incomplete), ensure_ascii=False)
        db.commit()
        db.refresh(row)
        return row

    row = IncidentConclusion(
        root_id=root_id.strip(),
        node_id=node_id.strip(),
        title=title or f"结论 {root_id}",
        status=status,
        facts_json=json.dumps(payload, ensure_ascii=False, default=str),
        incomplete_json=json.dumps(list(incomplete), ensure_ascii=False),
        authored=authored,
        markdown=markdown,
        author=author,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_conclusions(db: Any, *, root_id: str = "", limit: int = 50) -> list[Any]:
    """结论清单。默认最近在前 —— 复盘总是从最近的一次开始。"""
    from sqlalchemy import select

    from .models import IncidentConclusion

    query = select(IncidentConclusion).order_by(IncidentConclusion.id.desc()).limit(limit)
    if root_id:
        query = (
            select(IncidentConclusion)
            .where(IncidentConclusion.root_id == root_id)
            .order_by(IncidentConclusion.id.desc())
            .limit(limit)
        )
    return list(db.execute(query).scalars().all())


def payload(row: Any) -> dict[str, Any]:
    """给 API/界面的形状。`facts` / `incomplete` 从 JSON 还原。"""
    import json

    def _load(raw: str, fallback: Any) -> Any:
        try:
            return json.loads(raw) if raw else fallback
        except (TypeError, ValueError):
            return fallback

    return {
        "id": row.id,
        "root_id": row.root_id,
        "node_id": row.node_id,
        "title": row.title,
        "status": row.status,
        "author": row.author,
        "authored": row.authored,
        "markdown": row.markdown,
        "facts": _load(row.facts_json, {}),
        "incomplete": _load(row.incomplete_json, []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
