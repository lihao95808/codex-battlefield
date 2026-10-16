"""**目标投递与进度**（P4-1）：管家起树之后不再断线。

## 要解决的不是"能不能起树"，而是"起完就断线"

`start_task` 原先起完树就返回一个 `root_id`，之后管家与那棵树再无联系。用户于是只能
自己去翻看板："第几步了？卡在哪？" —— 而这两句话恰是管家唯一该回答的东西。

## 两条设计规矩

1. **进度不上报就等于没有**：这里不做后台推送，而是提供**只在被问到时才算**的
   `progress()`。原因是推送会把"树在动"变成消息流，而这个工具已经有看门狗与信号量
   在做后台判断；管家是对话者，它的报告应当发生在**有人看的时候**。
2. **"第几步"必须是可核对的**：不报"大概跑了一半"，而报
   `已完成 12 / 总计 30`，并给出**前线**（最深的未完成节点）与**卡点**
   （阻塞/失败/未清阻断债）。含糊的进度无法让人判断要不要介入。

## 与注意账（P4-3）的分工

注意账回答"这件事谁在处理"（防止两个组件重复动手）；本模块回答
"这棵树进展到哪、卡在哪"（让管家能开口讲）。前者是**协调**，后者是**汇报**。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# 完成的定义：只有 `done` 算完成。
# 为什么不把 `canceled` 也算完成：取消掉的分支**没有被做出来**，把它算进完成率
# 会让"进度 90%"读起来像"快好了"，而真相是"有一部分你决定不做了" —— 两者对
# 用户的下一步动作完全不同。
DONE_STATUSES = frozenset({"done"})
# 终态（不会再自己往下跑）
TERMINAL_STATUSES = frozenset({"done", "failed", "canceled", "blocked"})
# 正在干活（供调用方判断"树在不在动"；本模块的完成判定只认 `done`）
ACTIVE_STATUSES = frozenset({"planning", "running", "working", "verifying", "accepting"})
# 排队中
QUEUED_STATUSES = frozenset({"queued", "planned"})
# 卡住的（要人或有债）
STUCK_STATUSES = frozenset({"blocked", "failed"})

# 前线最多列几个节点。列多了没人读，而"卡在哪"通常就是那一两个。
FRONTIER_LIMIT = 5


def _status_of(task: Mapping[str, Any]) -> str:
    return str(task.get("status") or "")


def _depth_of(task: Mapping[str, Any]) -> int:
    try:
        return int(task.get("depth") or 0)
    except (TypeError, ValueError):
        return 0


def progress(
    tree: Mapping[str, Any],
    *,
    open_obligations: Sequence[Mapping[str, Any]] | None = None,
    goal: str = "",
    author: str = "",
) -> dict[str, Any]:
    """把一棵树的现状压成"第几步、卡在哪"。**纯计算。**

    `open_obligations=None` 表示**调用方没有去查债**（而不是"查了，没有债"）。
    两者的区别会写进 `incomplete` —— 把"没查"读成"没有"是这一层最容易犯的错。

    返回的关键字段：

    * `done` / `total` / `ratio`：完成度。**分母是节点数**，不是"工作量" ——
      没有可靠的工作量单位，硬造一个（比如按 token 折算）会比"数节点"更假。
    * `frontier`：**最深的未完成节点**（含状态）。这是"第几步"最接近的答案。
    * `stuck`：阻塞/失败节点 + 未清阻断债。这是"卡在哪"最接近的答案。
    * `next_step`：一句话说清**现在该看什么**。含糊的进度无法让人判断要不要介入。

    取不到的部分一律进 `incomplete` —— 不编。
    """
    tasks = list(tree.get("tasks") or [])
    blocked_by_dependency = open_obligations is None
    obligations = list(open_obligations or [])
    incomplete: list[str] = []

    if not tasks:
        return {
            "root_id": tree.get("root_id"),
            "goal": goal,
            "author": author,
            "available": False,
            "reason": "这棵树里没有节点 —— 要么它还没开始规划，要么读到的是另一棵树。",
            "done": 0,
            "total": 0,
            "ratio": None,
            "frontier": [],
            "stuck": [],
            "next_step": "先确认目标是否真的起树成功。",
            "incomplete": ["没有任何节点可读。"],
        }

    counts: dict[str, int] = {}
    for task in tasks:
        key = _status_of(task)
        counts[key] = counts.get(key, 0) + 1

    total = len(tasks)
    done = sum(count for status, count in counts.items() if status in DONE_STATUSES)

    # 前线 = 最深的未完成节点。取最深的几个而不是全部：用户要看的是"眼下推到哪了"。
    unfinished = [task for task in tasks if _status_of(task) not in TERMINAL_STATUSES]
    frontier = [
        {
            "id": str(task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "status": _status_of(task),
            "depth": _depth_of(task),
        }
        for task in sorted(unfinished, key=lambda item: (-_depth_of(item), str(item.get("id"))))[
            :FRONTIER_LIMIT
        ]
    ]

    stuck_nodes = [
        {
            "id": str(task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "status": _status_of(task),
            "error": str(task.get("error_message") or "")[:160],
        }
        for task in tasks
        if _status_of(task) in STUCK_STATUSES
    ]
    blocking = [
        {
            "id": str(item.get("id") or item.get("obligation_id") or ""),
            "type": str(item.get("type") or item.get("kind") or ""),
            "node_id": str(item.get("node_id") or item.get("source_node_id") or ""),
        }
        for item in obligations
        if str(item.get("severity") or "") == "blocking"
    ]

    if not tree.get("root_id"):
        incomplete.append("读不到 root_id —— 这份进度可能不属于你以为的那棵树。")
    if blocked_by_dependency:
        # 一条**设计上的**诚实点：进度依赖调用方把未清债传进来。
        # 不传时不报错（那是"没查"，不是"没有"），但也不能让读者以为"已经查过了"。
        incomplete.append(
            "未清债务没有一起读进来 —— 「卡在哪」那一栏可能少列了债，"
            "它不等于「没有债」。"
        )

    return {
        "root_id": tree.get("root_id"),
        "goal": goal,
        "author": author,
        "available": True,
        "reason": "",
        "done": done,
        "total": total,
        # 分母为 0 时给 None，不给 0 —— 0% 会被读成"一点没做"。
        "ratio": round(done / total, 4) if total else None,
        "status_counts": dict(sorted(counts.items())),
        "frontier": frontier,
        "stuck": stuck_nodes,
        "blocking_obligations": blocking,
        "next_step": next_step(
            done=done, total=total, frontier=frontier, stuck=stuck_nodes, blocking=blocking
        ),
        "incomplete": incomplete,
    }


def next_step(
    *,
    done: int,
    total: int,
    frontier: Sequence[Mapping[str, Any]],
    stuck: Sequence[Mapping[str, Any]],
    blocking: Sequence[Mapping[str, Any]],
) -> str:
    """一句话：现在该看什么。

    优先级刻意是"**要人动手的**优先"：有阻断债或卡住节点时先说那个，
    而不是先报"完成 12/30"。否则用户会以为一切正常，直到很久之后才发现有人一直在等。
    """
    if blocking:
        return f"有 {len(blocking)} 条未清阻断债在等人处理 —— 先看这个。"
    if stuck:
        first = stuck[0]
        return f"{len(stuck)} 个节点卡住了（如 {first.get('id')}：{first.get('status')}）—— 先看这个。"
    if done >= total:
        return "所有节点都已结算。"
    if frontier:
        first = frontier[0]
        return f"正在推进最深的那一层（如 {first.get('id')}，第 {first.get('depth')} 层）。"
    return "没有未完成节点，但也没有完成记录 —— 数据可能不一致。"


def summary_line(snapshot: Mapping[str, Any]) -> str:
    """给管家/界面的一行话。**不可用时说明原因，不报 0/0。**"""
    if not snapshot.get("available"):
        return f"进度读不到：{snapshot.get('reason') or '原因未给出'}"
    ratio = snapshot.get("ratio")
    percent = "—" if ratio is None else f"{round(float(ratio) * 100)}%"
    head = f"第 {snapshot.get('done')}/{snapshot.get('total')} 个节点已完成（{percent}）"
    return f"{head}；{snapshot.get('next_step')}"


def record(
    db: Any,
    *,
    root_id: str,
    goal: str,
    author: str = "",
    source: str = "steward_action",
) -> Any:
    """记一次目标投递。

    **要求 `root_id` 非空**：一次"投递"的定义就是"确实起出了一棵树"。
    起树失败时不该留下一条投递记录 —— 那会让"我投过这个目标"看起来像发生过。
    """
    from .models import GoalDelivery

    if not str(root_id or "").strip():
        raise ValueError("投递记录必须带 root_id —— 起树失败就不该留下一条'我投过'。")
    row = GoalDelivery(
        root_id=str(root_id).strip(),
        goal=str(goal or "").strip(),
        author=str(author or "").strip(),
        source=str(source or "steward_action").strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def latest(db: Any, *, author: str = "") -> Any:
    """最近一次投递。给了 `author` 就只看那个人的 —— 多人共用一台机器时很重要。"""
    from sqlalchemy import select

    from .models import GoalDelivery

    query = select(GoalDelivery).order_by(GoalDelivery.id.desc()).limit(1)
    if author:
        query = (
            select(GoalDelivery)
            .where(GoalDelivery.author == author)
            .order_by(GoalDelivery.id.desc())
            .limit(1)
        )
    return db.execute(query).scalars().first()


def payload(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "root_id": row.root_id,
        "goal": row.goal,
        "author": row.author,
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
