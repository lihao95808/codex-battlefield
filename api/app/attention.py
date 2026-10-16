"""**注意账**：管家与看门狗之间"这件事我在处理"的唯一记录（P4-3）。

## 要解决的问题

管家和看门狗都能发现问题，但**谁也不知道对方已经看到了**。于是同一件事被处理两次：

* 看门狗把僵死节点重新排队，管家同一时刻也建议重试 —— 两次重试；
* 管家在等用户确认"要不要取消这棵树"，看门狗看它没进度又休眠一次。

重复动作的代价不只是白干活：它会**掩盖真实信号**（"这个节点被重试了 3 次"
看起来像节点自己不稳定，其实是两个组件各点了一次）。

## 一条规矩就够了

> **谁要处理，谁记账；处理前先查账。**

`claim()` 是幂等的：同一个 `(kind, target)` 重复认领只刷新时间，不产生第二条。
这比"每次动作前去重"更可靠 —— 去重的规则会随动作种类增长，而账只有一份。

## 三条设计取舍

1. **只有认领人自己能放掉**（`release(actor=...)` 校验）。这是**权限边界**：
   看门狗不该替管家宣布"管家的介入结束了"。人可以在界面上强制放掉 ——
   否则一条卡住的记录会把某个目标永久锁死，而"永久锁死"比重复动作更糟。
2. **过期即失效**（`expires_at`）。没有过期的账会成为永久阻塞：一个忘记释放的认领
   会让看门狗从此对那个目标闭眼，而看板上看起来一切正常。
3. **`kind` 用封闭词表**。自由字符串会让"同一件事"被记成两个 kind，
   账本于是形同虚设。未登记的 kind 直接被拒（而不是静默接受）。

## 与"债务"的区别（容易混，写明）

| | 债务（obligation） | 注意账（claim） |
|---|---|---|
| 回答 | 这棵树**还欠什么** | 这件事**谁在处理** |
| 谁看 | 门禁（能不能标完成） | 管家 / 看门狗（要不要动手） |
| 有效期 | 不会自动过期 | **必须过期** |

一张过期的债务是"还没做完"；一张过期的认领是"没人管了，谁都可以接手"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# 认领人。**封闭词表** —— 新增一方必须显式登记，否则"谁在处理"这件事
# 会变成一堆自由字符串，账本也就没法用来判断重复。
WATCHDOG = "watchdog"
STEWARD = "steward"
HUMAN = "human"
ACTORS: tuple[str, ...] = (WATCHDOG, STEWARD, HUMAN)

ACTOR_LABEL: dict[str, str] = {
    WATCHDOG: "看门狗（规则自动）",
    STEWARD: "管家（对话/建议）",
    HUMAN: "人",
}

# 注意类型。**封闭词表**：自由字符串会让"同一件事"被记成两个 kind。
KINDS: tuple[str, ...] = (
    "stalled_agent",  # 僵死进程
    "resource_pressure",  # 机器压力
    "budget_gate",  # 额度闸
    "blocked_subtree",  # 阻塞子树
    "human_decision",  # 等人做决定
    "requeue",  # 重新排队
    "hibernation",  # 休眠/恢复
)

KIND_LABEL: dict[str, str] = {
    "stalled_agent": "僵死进程",
    "resource_pressure": "机器压力",
    "budget_gate": "额度闸",
    "blocked_subtree": "阻塞子树",
    "human_decision": "等人决定",
    "requeue": "重新排队",
    "hibernation": "休眠/恢复",
}

# 默认认领时长。太长会让"没人管了"迟迟不暴露；太短会让正常的处理被反复打断。
DEFAULT_TTL_SECONDS = 3600
# 上限：一次认领最多 24 小时。没有上限时"永久认领"只是把 TTL 写大一点。
MAX_TTL_SECONDS = 86400


class ClaimError(ValueError):
    """认领不合法。调用方当 4xx 报出去。"""


@dataclass(frozen=True)
class ClaimDecision:
    """一次认领判定的结果。**无论允许与否都给 `reason`。**"""

    allowed: bool
    claim_id: int | None
    reason: str
    # 允许且是"我在续期"时为真；允许且是新认领时为假。
    refreshed: bool = False


def validate_kind(kind: str) -> str:
    key = str(kind or "").strip()
    if key not in KINDS:
        raise ClaimError(f"未知的注意类型 {kind!r}。可选：{'、'.join(KINDS)}")
    return key


def validate_actor(actor: str) -> str:
    key = str(actor or "").strip().lower()
    if key not in ACTORS:
        raise ClaimError(f"未知的认领人 {actor!r}。可选：{'、'.join(ACTORS)}")
    return key


def validate_ttl(seconds: Any) -> int:
    """时长必须是正的整数，且不超过上限。**没有"永久认领"。**"""
    try:
        value = int(seconds)
    except (TypeError, ValueError) as exc:
        raise ClaimError(f"认领时长必须是整数秒，收到 {seconds!r}。") from exc
    if value <= 0:
        raise ClaimError("认领时长必须是正数 —— 一条不过期的认领会让看门狗永久闭眼。")
    if value > MAX_TTL_SECONDS:
        raise ClaimError(
            f"认领时长最长 {MAX_TTL_SECONDS} 秒（24 小时）—— 更长的认领等于永久阻塞。"
        )
    return value


def _expires_at(now: datetime, seconds: int) -> datetime:
    return now + timedelta(seconds=seconds)


def claim(
    db: Any,
    *,
    kind: str,
    target: str,
    actor: str,
    reason: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> ClaimDecision:
    """认领一件事。**幂等**：同一个 `(kind, target)` 重复认领只刷新时长。

    这是 P4-3 的核心入口 —— 管家和看门狗在动手之前都先来这里。

    判定顺序（每条都有独立理由，因为"为什么被拒"要能查到）：

    1. 类型/认领人合法；
    2. 已有**未过期**的认领属于**别人** → 拒绝（这就是防重复动作的那条）；
    3. 已有未过期的认领属于**自己** → 允许，标记 `refreshed=True`；
    4. 没有认领 / 已过期 → 允许，新建或接管。
    """
    from .models import AttentionClaim, utcnow

    kind_key = validate_kind(kind)
    actor_key = validate_actor(actor)
    ttl = validate_ttl(ttl_seconds)
    moment = now or utcnow()
    scope = str(target or "").strip()

    existing = _active_row(db, kind_key, scope, moment)
    if existing is not None and existing.actor != actor_key:
        return ClaimDecision(
            allowed=False,
            claim_id=existing.id,
            reason=(
                f"{KIND_LABEL.get(kind_key, kind_key)} / {scope or '（全部）'} 已由"
                f"{ACTOR_LABEL.get(existing.actor, existing.actor)}处理中"
                f"（{existing.reason or '未写理由'}）—— 不要重复动手。"
            ),
        )

    if existing is not None:
        existing.expires_at = _expires_at(moment, ttl)
        existing.reason = reason or existing.reason
        db.commit()
        db.refresh(existing)
        return ClaimDecision(
            allowed=True,
            claim_id=existing.id,
            refreshed=True,
            reason=f"你在续期这条认领（{ttl} 秒后过期）。",
        )

    row = AttentionClaim(
        kind=kind_key,
        target=scope,
        actor=actor_key,
        reason=reason,
        claimed_at=moment,
        expires_at=_expires_at(moment, ttl),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ClaimDecision(
        allowed=True,
        claim_id=row.id,
        reason=f"已认领，{ttl} 秒后过期（过期后谁都可以接手）。",
    )


def _active_row(db: Any, kind: str, target: str, now: datetime) -> Any:
    """同 `(kind, target)` 上**未过期**的那条认领（没有则 None）。"""
    from sqlalchemy import select

    from .models import AttentionClaim

    rows = (
        db.execute(
            select(AttentionClaim)
            .where(AttentionClaim.kind == kind)
            .where(AttentionClaim.target == target)
            .order_by(AttentionClaim.id.desc())
        )
        .scalars()
        .all()
    )
    for row in rows:
        if _aware(row.expires_at) > _aware(now):
            return row
    return None


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(0, tz=timezone.utc)


def release(
    db: Any,
    claim_id: int,
    *,
    actor: str,
    force: bool = False,
    reason: str = "",
    declared_by: str = "",
) -> dict[str, Any]:
    """放掉一条认领。

    **只有认领人自己能放**，除非 `force=True` 且 `declared_by` 非空（人在界面上强制解除）。
    这是权限边界：看门狗不该替管家宣布"管家的介入结束了"。

    强制解除必须留名 —— 否则"谁把这条锁解开的"在事后无从查起。
    """
    from .models import AttentionClaim

    actor_key = validate_actor(actor)
    row = db.get(AttentionClaim, int(claim_id))
    if row is None:
        raise ClaimError(f"找不到认领 {claim_id}。")

    if row.actor != actor_key and not force:
        raise ClaimError(
            f"这条认领属于{ACTOR_LABEL.get(row.actor, row.actor)}，"
            f"{ACTOR_LABEL.get(actor_key, actor_key)}不能替它解除。"
            "如果确实要强制解除，请在界面上操作（会留名）。"
        )
    if row.actor != actor_key and force and not declared_by.strip():
        raise ClaimError("强制解除别人的认领必须留名（declared_by）。")

    row.released_at = _now()
    row.released_by = declared_by.strip() or actor_key
    row.release_reason = reason
    db.commit()
    return {
        "status": "released",
        "claim_id": row.id,
        "forced": row.actor != actor_key,
        "released_by": row.released_by,
        "message": (
            f"已解除 {KIND_LABEL.get(row.kind, row.kind)} / {row.target or '（全部）'} 的认领"
            + ("（强制解除，已留名）。" if row.actor != actor_key else "。")
        ),
    }


def _now() -> Any:
    from .models import utcnow

    return utcnow()


def active_claims(db: Any, *, now: datetime | None = None) -> list[Any]:
    """当前生效的认领（未释放且未过期）。"""
    from sqlalchemy import select

    from .models import AttentionClaim

    moment = now or _now()
    rows = db.execute(select(AttentionClaim).order_by(AttentionClaim.id.desc())).scalars().all()
    return [row for row in rows if row.released_at is None and _aware(row.expires_at) > _aware(moment)]


def is_claimed_by_other(
    db: Any, *, kind: str, target: str, actor: str, now: datetime | None = None
) -> tuple[bool, str]:
    """`(是否被别人认领, 理由)`。

    给看门狗/管家动手之前的**一个**判断入口：调用方不需要理解账本的细节，
    只要问"这件事现在能不能我来做"。
    """
    kind_key = validate_kind(kind)
    actor_key = validate_actor(actor)
    row = _active_row(db, kind_key, target, now or _now())
    if row is None or row.actor == actor_key:
        return False, ""
    return True, (
        f"{ACTOR_LABEL.get(row.actor, row.actor)}正在处理"
        f"（{row.reason or '未写理由'}），本次不动手。"
    )


def payload(db: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """给 API/界面的形状。**含标签**，因为 `kind` 是内部词。"""
    rows = active_claims(db, now=now)
    items = [
        {
            "id": row.id,
            "kind": row.kind,
            "kind_label": KIND_LABEL.get(row.kind, row.kind),
            "target": row.target,
            "actor": row.actor,
            "actor_label": ACTOR_LABEL.get(row.actor, row.actor),
            "reason": row.reason,
            "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "remaining_seconds": max(
                0, int((_aware(row.expires_at) - _aware(now or _now())).total_seconds())
            ),
        }
        for row in rows
    ]
    return {
        "claims": items,
        "count": len(items),
        "actors": [{"actor": key, "label": ACTOR_LABEL[key]} for key in ACTORS],
        "kinds": [{"kind": key, "label": KIND_LABEL[key]} for key in KINDS],
        "note": (
            "谁要处理谁记账，处理前先查账。认领会过期 —— 过期的认领意味着"
            "「没人管了，谁都可以接手」，这与「还在处理」必须分得清。"
        ),
    }
