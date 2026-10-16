"""**可调档位的审计与回滚**（P4-2）。

## 要解决的不是"能不能调"，而是"调了之后查得到"

看门狗的并发档、额度闸、休眠窗口本来就能通过配置改。缺的是三样东西：

1. **谁在什么时候改了什么**（含改前的值）—— 否则事后只能看到"现在是 5"，
   而"以前是几、为什么变成 5"无从查起；
2. **能不能回滚** —— 一次调错的并发档会让机器被压垮，而"改回去"要能一键做到；
3. **改的理由** —— 审计里最有价值的一列。没有理由的记录只能回答"谁改的"。

## 为什么复用事件表而不是新建审计表

`event_records` 已经是"发生了什么"的权威记录，而且看板/结论/诊断包都在读它。
再开一张审计表会让同一个改动出现两处，且**两处会慢慢不一致**（一处补了、一处忘了）。
所以这里只做两件事：把改动**规范化**成一条可读的记录，并提供**回滚**所需的旧值。

## 一条规矩

> **记录先于生效**：`apply()` 先写事件、再改配置。

反过来的话，"改了但没记上"会在崩溃时留下一个无人知道的改动 —— 而它看起来
和"配置一直都是这样"完全一样。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# 可调档位的**封闭登记表**。自由键名会让审计变成"改了一堆东西但说不清改了哪一项"，
# 而且回滚时不知道该往哪写。
#
# `kind` 决定值怎么解析（界面据此选控件），`minimum`/`maximum` 是硬边界：
# 越界的值不是"奇怪"而是"危险"（并发 0 = 静默停摆，并发 999 = 压垮机器）。
REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "key": "max_concurrency",
        "env": "CODEX_BATTLEFIELD_MAX_CONCURRENCY",
        "label": "最大并发",
        "kind": "int",
        "minimum": 1,
        "maximum": 64,
        "rationale": "同时跑多少个 agent。上限存在的意义是别把机器压垮。",
    },
    {
        "key": "auto_concurrency_enabled",
        "env": "CODEX_BATTLEFIELD_AUTO_CONCURRENCY_ENABLED",
        "label": "自动并发",
        "kind": "bool",
        "rationale": "让系统按排队/内存压力自动调并发。关掉就固定用「最大并发」。",
    },
    {
        "key": "auto_concurrency_min",
        "env": "CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MIN",
        "label": "自动并发下限",
        "kind": "int",
        "minimum": 1,
        "maximum": 64,
        "rationale": "自动调并发的下界。设成 0 等于允许静默停摆。",
    },
    {
        "key": "auto_concurrency_max",
        "env": "CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MAX",
        "label": "自动并发上限",
        "kind": "int",
        "minimum": 1,
        "maximum": 64,
        "rationale": "自动调并发的上界。",
    },
    {
        "key": "agent_burst_limit",
        "env": "CODEX_BATTLEFIELD_AGENT_BURST_LIMIT",
        "label": "本轮 agent 上限",
        "kind": "int",
        "minimum": 0,
        "maximum": 1000,
        "rationale": "一轮里最多启动多少个 agent。0 = 不限制。",
    },
    {
        "key": "agent_burst_reserve",
        "env": "CODEX_BATTLEFIELD_AGENT_BURST_RESERVE",
        "label": "保留额度",
        "kind": "int",
        "minimum": 0,
        "maximum": 1000,
        "rationale": "留出多少额度不自动用完 —— 防的是把额度烧到一分不剩。",
    },
    {
        "key": "codex_idle_timeout_seconds",
        "env": "CODEX_BATTLEFIELD_CODEX_IDLE_TIMEOUT_SECONDS",
        "label": "空闲判定（秒）",
        "kind": "int",
        "minimum": 60,
        "maximum": 86400,
        "rationale": "多久没进度算僵死。太小会打断正常长跑，太大会让僵死藏很久。",
    },
    {
        "key": "machine_safety_min_available_memory_mb",
        "env": "CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB",
        "label": "机器安全水位（MB）",
        "kind": "int",
        "minimum": 0,
        "maximum": 262144,
        "rationale": "可用内存低于它就休眠。0 = 关掉这条守卫。",
    },
)

_BY_KEY = {item["key"]: item for item in REGISTRY}

EVENT_TYPE = "watchdog.tuning"

# 危险方向：这些改动会**放宽**限制或**增加**开销，界面上要二次确认。
#
# 判据不是"值变大了"，而是"这个键往哪个方向变危险" —— 例如
# `machine_safety_min_available_memory_mb` 调**小**才是危险（守卫变松）。
RISKY_DIRECTION: dict[str, str] = {
    "max_concurrency": "up",
    "auto_concurrency_max": "up",
    "agent_burst_limit": "up",
    "machine_safety_min_available_memory_mb": "down",
    "codex_idle_timeout_seconds": "up",
}


class TuningError(ValueError):
    """档位改动不合法。调用方当 4xx 报出去。"""


def catalog() -> list[dict[str, Any]]:
    """可调档位清单（界面据此渲染表单）。"""
    return [
        {
            "key": item["key"],
            "env": item["env"],
            "label": item["label"],
            "kind": item["kind"],
            "minimum": item.get("minimum"),
            "maximum": item.get("maximum"),
            "rationale": item["rationale"],
            "risky_direction": RISKY_DIRECTION.get(item["key"]),
        }
        for item in REGISTRY
    ]


def coerce(key: str, value: Any) -> Any:
    """把界面给的值转成正确类型，并做**硬边界**校验。

    边界不是建议：并发 0 是静默停摆，并发 999 是压垮机器。两者都不是"用户的自由"。
    """
    spec = _BY_KEY.get(str(key))
    if spec is None:
        raise TuningError(f"未知档位 {key!r}。可选：{'、'.join(_BY_KEY)}")

    if spec["kind"] == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise TuningError(f"{spec['label']} 只接受 true/false，收到 {value!r}。")

    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TuningError(f"{spec['label']} 必须是整数，收到 {value!r}。") from exc
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    if minimum is not None and number < minimum:
        raise TuningError(f"{spec['label']} 不能小于 {minimum}（{spec['rationale']}）")
    if maximum is not None and number > maximum:
        raise TuningError(f"{spec['label']} 不能大于 {maximum}（{spec['rationale']}）")
    return number


def is_risky(key: str, old: Any, new: Any) -> bool:
    """这次改动是不是往危险方向走。

    按**方向**判断，不是按"值变了"：机器安全水位调**小**才危险（守卫变松），
    调大是收紧。把方向搞反会让界面对收紧也弹确认，于是人开始无脑点"确定"。
    """
    direction = RISKY_DIRECTION.get(key)
    if direction is None or old is None:
        return False
    try:
        if direction == "up":
            return float(new) > float(old)
        return float(new) < float(old)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class Change:
    """一条档位改动的**完整记录**。含旧值 —— 没有旧值就没法回滚。"""

    key: str
    old: Any
    new: Any
    actor: str
    reason: str
    risky: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": _BY_KEY[self.key]["label"],
            "old": self.old,
            "new": self.new,
            "actor": self.actor,
            "reason": self.reason,
            "risky": self.risky,
        }


def plan(changes: list[dict[str, Any]], *, current: dict[str, Any], actor: str) -> list[Change]:
    """把请求规范化成改动清单。

    **先全部校验再返回**：一半生效一半失败是最难查的状态（用户只看到一条错误，
    而实际改了两项里的第一项）。
    """
    if not actor.strip():
        raise TuningError("调档必须留名（actor）—— 否则事后无法回答是谁改的。")
    planned: list[Change] = []
    seen: set[str] = set()
    for item in changes or []:
        key = str((item or {}).get("key") or "").strip()
        if key in seen:
            raise TuningError(f"同一个档位在一次请求里出现了两次：{key}")
        seen.add(key)
        new_value = coerce(key, (item or {}).get("value"))
        old_value = current.get(key)
        planned.append(
            Change(
                key=key,
                old=old_value,
                new=new_value,
                actor=actor.strip(),
                reason=str((item or {}).get("reason") or "").strip(),
                risky=is_risky(key, old_value, new_value),
            )
        )
    if not planned:
        raise TuningError("没有要改的档位。")
    return planned


def render(changes: list[Change]) -> str:
    """人话记录。进事件表与审计列表的都是这一段。"""
    parts = []
    for change in changes:
        label = _BY_KEY[change.key]["label"]
        marker = "（放宽，危险方向）" if change.risky else ""
        parts.append(f"{label} {change.old} → {change.new}{marker}")
    return "；".join(parts)


def record(db: Any, changes: list[Change], *, applied: bool) -> None:
    """把改动写进事件表。**记录先于生效**（调用方保证顺序）。

    为什么复用事件表：它已经是"发生了什么"的权威记录，看板/结论/诊断包都在读它。
    再开一张审计表会让同一个改动出现两处，而两处会慢慢不一致。

    写法与编排层一致（`EventRecord` + `event_bus` 推送），这样才能被看板事件流看到 ——
    只写库不推送的记录在界面上不会出现，而"改了但界面没提示"正是要避免的。

    ⚠️ `event_bus.publish` 是**协程**，而且这里可能跑在没有事件循环的线程里
    （看门狗是独立进程/线程）。所以：**有循环就调度，没有就只写库**（与
    `orchestrator._add_event` 同一套写法）。直接 `event_bus.publish(...)` 会留下
    `coroutine was never awaited` 的警告 —— 实测踩到过，而且那种警告很容易被忽略。
    """
    import asyncio

    from .events import event_bus
    from .models import EventRecord

    text = render(changes)
    if not applied:
        text = f"[已回滚] {text}"
    message = f"档位调整（{changes[0].actor}）：{text}"
    row = EventRecord(
        task_id=None,
        type=EVENT_TYPE,
        message=message,
        payload_json=json.dumps(
            {
                "applied": applied,
                "actor": changes[0].actor,
                "reason": changes[0].reason,
                "changes": [change.as_payload() for change in changes],
            },
            ensure_ascii=False,
        ),
    )
    db.add(row)
    db.commit()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(event_bus.publish(json.dumps({"type": EVENT_TYPE, "message": message})))


def rollback_targets(history: list[dict[str, Any]], *, limit: int = 1) -> list[Change]:
    """从审计历史里算出"回滚到哪一步"。

    取最近 `limit` 次改动的**旧值**作为目标。为什么要能回滚：
    一次调错的并发档会让机器被压垮，而"改回去"必须是一键的 ——
    否则人会去猜原来的值，猜错的概率不低。

    ## 同一个档位出现多次时，取**最早**那个旧值

    回滚 N 步意味着"回到这批改动之前"，所以目标是这批里**最早那次**的旧值。
    第一版直接逐条收集，于是"回滚 2 步"在同一档位上产生了两条 `Change`，
    `plan()` 的重复键校验当场拒绝 —— 也就是说**多步回滚曾经完全不可用**，
    而界面上的表现只是一句"同一个档位出现了两次"。实测由端点判据抓到。
    """
    targets: dict[str, Change] = {}
    for entry in history[:limit]:
        for item in entry.get("changes") or []:
            key = str(item.get("key") or "")
            if key not in _BY_KEY:
                continue
            # 后面的（更早的）赋值覆盖前面的：最后留下的是这批里最早那次的旧值。
            targets[key] = Change(
                key=key,
                old=item.get("new"),
                new=item.get("old"),
                actor=str(entry.get("actor") or ""),
                reason=f"回滚：{item.get('label') or key} {item.get('new')} → {item.get('old')}",
            )
    return list(targets.values())


def history(db: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """档位调整的审计历史（最近在前）。"""
    from sqlalchemy import select

    from .models import EventRecord

    rows = (
        db.execute(
            select(EventRecord.payload_json, EventRecord.message, EventRecord.created_at)
            .where(EventRecord.type == EVENT_TYPE)
            .order_by(EventRecord.id.desc())
            .limit(limit)
        )
        .all()
    )
    items: list[dict[str, Any]] = []
    for payload_json, message, created_at in rows:
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items.append(
            {
                "at": created_at.isoformat() if created_at else None,
                "message": str(message or ""),
                "applied": bool(payload.get("applied", True)),
                "actor": str(payload.get("actor") or ""),
                "reason": str(payload.get("reason") or ""),
                "changes": payload.get("changes") or [],
            }
        )
    return items


def current_values(settings: Any) -> dict[str, Any]:
    """当前生效值（界面用它显示"改之前是什么"）。

    只读 `Settings` 上**真的会被读**的那些字段 —— 登记表里的键如果 `Settings` 上没有，
    那就是登记错了，这里会 KeyError 并在测试里被抓住，而不是静默显示一个默认值。
    """
    return {item["key"]: getattr(settings, item["key"]) for item in REGISTRY}


def apply_to_config_file(changes: list[Change]) -> None:
    """把改动写进用户配置文件。

    与 P2-2 的设置面板走**同一条路**（`set_config_value`）—— 两条写路径会漂移，
    而漂移的表现是"面板里改了、这里改了、结果只剩一个生效"。
    """
    from .state_layout import set_config_value

    for change in changes:
        set_config_value(change.key, change.new)
