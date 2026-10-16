"""管家的对话服务：一轮对话怎么跑、历史怎么存。`docs/spec-steward.md`。

## 一轮对话的形状

    用户消息
      -> 取状态快照（只读）        steward_status.build_status
      -> 取能力目录（含权限）      steward_capabilities.catalog
      -> 取最近若干轮对话          read_history
      -> 交给 runner（codex）      runner.steward_chat
      -> 校验 payload              StewardChat
      -> 落库（用户 + 管家两条）    append_message
      -> 返回给界面

## 为什么历史存我们库里、而不是靠 codex 的 session 续接

codex 侧我们开了 `--ephemeral`（不留 session）。历史由我们拼进提示词，
好处是**可审计**：谁说了什么、agent 当时看到的是哪份快照，都落盘。
靠 session 续接就等于把上下文放在别人的状态里，出问题时查不到。

## 为什么快照每轮都重建

管家要回答"现在怎么样"。缓存快照会让它拿着旧状态回答新问题 ——
而这种错误极难发现（它的回答看起来永远合理）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import StewardMessage
from .schemas import StewardChat
from .steward_capabilities import catalog
from .steward_status import build_status

ROLE_USER = "user"
ROLE_STEWARD = "steward"

# 管家**自己开口**的提示词（不是用户问的）。
#
# 为什么要让它主动说话：一个只会应答的东西不是"管家"，是个查询界面。
# 真正有价值的是它在出事时**先开口** —— 树卡住了、看门狗停摆了、
# 有动作等决定，这些都不该等用户想起来问。
#
# 但主动开口有个硬约束：**不能打扰**。所以触发条件在 `steward_proactive`
# 里是"重要边沿"（见那里的注释），而不是"有任何变化"。
PROACTIVE_INSTRUCTION = (
    "（系统）以下情况刚发生变化，请**主动**向用户汇报：{trigger}\n"
    "要求：一、只说这次变化本身和它对用户意味着什么，别复述整个状态；"
    "二、如果建议做点什么，用 proposed_actions 提出来（我不能自己执行，需要用户批准）；"
    "三、如果没什么可做的，就一句话说明现状、不要凑字数。"
)

# 拼进提示词的历史轮数。不取全部：`--ephemeral` 下每轮都要把历史重发一遍，
# 太长会吃光上下文预算，也会让重点被淹掉。
HISTORY_WINDOW = 12
# 单条历史在提示词里最多留多少字符（防止某轮粘了长文把后面挤掉）。
HISTORY_ITEM_CHARS = 1200


def recent_messages(db: Session, *, limit: int = HISTORY_WINDOW) -> list[StewardMessage]:
    """最近的对话，**按时间正序**返回（提示词里要按说话顺序读）。"""
    rows = (
        db.execute(select(StewardMessage).order_by(StewardMessage.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return list(reversed(rows))


def render_history(rows: list[StewardMessage]) -> str:
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows:
        speaker = "用户" if row.role == ROLE_USER else "管家"
        text = (row.content or "")[:HISTORY_ITEM_CHARS]
        lines.append(f"{speaker}：{text}")
    return "\n".join(lines)


def append_message(
    db: Session, *, role: str, content: str, metadata: dict[str, Any] | None = None
) -> StewardMessage:
    row = StewardMessage(
        role=role,
        content=content,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def clear_history(db: Session) -> int:
    """清空对话。返回删了几条。

    **只清对话**，不碰树、不碰其他状态 —— 用户说"忘掉我们聊的"时，
    不该顺手把任务也弄没了。
    """
    rows = db.execute(select(StewardMessage.id)).scalars().all()
    db.execute(delete(StewardMessage))
    db.commit()
    return len(rows)


def parse_chat(payload: Any) -> StewardChat:
    """把 runner 的 payload 变成契约对象。

    校验失败**不抛到界面上**：管家是给人用的对话，一次 schema 不符不该变成
    500。退化成"我能说的只有原始输出"并把原因放进 metadata，用户至少能看到发生了什么。
    """
    try:
        return StewardChat.model_validate(payload)
    except Exception:  # noqa: BLE001 - 契约不符时必须还能回话
        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("reply") or payload.get("text") or "")
        if not text:
            text = "（管家这一轮的输出不符合约定格式，无法解析。原始输出已记录，可查 steward 的运行日志。）"
        return StewardChat(reply=text, situation="", confidence="low")


def build_context(db: Session, settings: Any, user_message: str, history_text: str) -> tuple[str, str]:
    """拼出喂给 runner 的两块文本：状态快照、能力目录。

    ## 为什么状态快照里要带"投递进度"（P4-1）

    管家起完树就与那棵树失去联系，于是用户问"第几步了、卡在哪"时它只能含糊其辞。
    把**最近一次投递**的进度塞进快照，它才能开口讲这两件事 —— 而"直接读最新状态"
    比"维护一份增量推送"更不容易出错：不存在漏推、也不存在状态漂移。
    """
    snapshot = build_status(db, settings)
    snapshot["delivery"] = _delivery_block(db)
    status_text = json.dumps(snapshot, ensure_ascii=False, indent=2)
    caps = catalog(read_only=bool(settings.steward_read_only))
    caps_text = json.dumps(caps, ensure_ascii=False, indent=2)
    return status_text, caps_text


def _delivery_block(db: Session) -> dict[str, Any]:
    """最近一次投递的进度。**没有投递过就如实说没有**，不编一棵树出来。"""
    from . import delivery
    from .models import TaskNode

    row = delivery.latest(db)
    if row is None:
        return {"available": False, "reason": "还没有通过管家投递过目标。"}

    tasks = db.query(TaskNode).all()
    tree_tasks = [
        {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "depth": task.depth,
            "error_message": task.error_message,
        }
        for task in tasks
    ]
    # 债读不到时传 `None`（"没查"），而不是空列表（"查了，没有"）。
    try:
        from . import obligation_index

        board = obligation_index.board(db, status="open")
        items = board.get("items") if isinstance(board, dict) else None
        obligations: list[dict[str, Any]] | None = list(items) if isinstance(items, list) else None
    except Exception:  # noqa: BLE001 - 索引坏了不该让管家的上下文整个拼不出来
        obligations = None

    snapshot = delivery.progress(
        # `root_id` 必须显式补上：它不是树的一部分，而是查询参数。
        {"root_id": row.root_id, "tasks": tree_tasks},
        open_obligations=obligations,
        goal=row.goal,
        author=row.author,
    )
    snapshot["delivery_id"] = row.id
    snapshot["delivered_at"] = row.created_at.isoformat() if row.created_at else None
    snapshot["summary"] = delivery.summary_line(snapshot)
    return snapshot
