"""注意账的判据（P4-3）。

这张账要解决的是**重复动作**：管家和看门狗都能发现问题，但谁也不知道对方已经看到了。
重复动作的代价不只是白干活 —— 它会**掩盖真实信号**（"这个节点被重试了 3 次"看起来
像节点不稳定，其实是两个组件各点了一次）。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| T1 | 别人已认领 → **拒绝**，且理由点名是谁在处理 | 同一件事被处理两次 |
| T2 | 自己已认领 → 允许（**续期**），不产生第二条 | 自己的重试被自己挡住 |
| T3 | 认领**必须过期**；过期的认领谁都能接手 | 一条忘释放的认领让看门狗永久闭眼 |
| T4 | 只有认领人能释放；强制释放**必须留名** | 权限边界被越过，且事后查不到是谁解的 |
| T5 | `kind` / `actor` 是封闭词表，未登记直接拒 | 同一件事被记成两个 kind，账本形同虚设 |
| T6 | 时长必须为正且有上限 | "永久认领"只是把 TTL 写大一点 |
| T7 | `payload` 带标签与剩余秒数 | 界面只能显示内部词，用户看不懂 |
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import attention
from app.database import SessionLocal, engine, init_db
from app.models import Base


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


def _claim(**over):
    params = {
        "kind": "stalled_agent",
        "target": "node-1",
        "actor": attention.WATCHDOG,
        "reason": "进程活着但没有进度",
    }
    params.update(over)
    with SessionLocal() as db:
        return attention.claim(db, **params)


MOMENT = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


# ---------- T1 别人已认领就拒绝 ----------


def test_t1_a_second_actor_is_refused_and_told_who_owns_it() -> None:
    """**这条是 P4-3 的全部要点**：同一件事不许被处理两次。"""
    first = _claim(actor=attention.WATCHDOG)
    assert first.allowed is True

    with SessionLocal() as db:
        second = attention.claim(
            db, kind="stalled_agent", target="node-1", actor=attention.STEWARD, reason="我也想重试"
        )

    assert second.allowed is False
    assert second.claim_id == first.claim_id
    assert "看门狗" in second.reason, "拒绝理由要点名是谁在处理"
    assert "不要重复动手" in second.reason


def test_t1_a_different_target_is_not_blocked() -> None:
    """认领是按 `(kind, target)` 的 —— 别的目标不受影响。"""
    _claim(target="node-1")
    with SessionLocal() as db:
        other = attention.claim(
            db, kind="stalled_agent", target="node-2", actor=attention.STEWARD
        )
    assert other.allowed is True


def test_t1_a_different_kind_on_the_same_target_is_not_blocked() -> None:
    """同一目标上不同类型的注意是两件事（僵死 vs 等人决定）。"""
    _claim(kind="stalled_agent", target="node-1")
    with SessionLocal() as db:
        other = attention.claim(
            db, kind="human_decision", target="node-1", actor=attention.STEWARD
        )
    assert other.allowed is True


def test_t1_is_claimed_by_other_is_the_single_entry_point() -> None:
    """给调用方的**一个**判断入口：不需要理解账本细节。"""
    _claim(actor=attention.WATCHDOG)
    with SessionLocal() as db:
        taken, why = attention.is_claimed_by_other(
            db, kind="stalled_agent", target="node-1", actor=attention.STEWARD
        )
        mine, _ = attention.is_claimed_by_other(
            db, kind="stalled_agent", target="node-1", actor=attention.WATCHDOG
        )
    assert taken is True and "看门狗" in why
    assert mine is False


# ---------- T2 续期 ----------


def test_t2_the_same_actor_refreshes_instead_of_creating_a_second_claim() -> None:
    """同一个认领人在**未过期**时再认领 = 续期，不产生第二条。

    ⚠️ 两次调用必须用**同一个冻结时刻**。第一版这里传了"一年后"的时间戳，
    于是原认领已过期 —— 那走的是"过期的认领谁都能接手"那条路（合理行为），
    测的根本不是续期。它当场红了，这正是它该做的事。
    """
    with SessionLocal() as db:
        first = attention.claim(
            db,
            kind="stalled_agent",
            target="node-1",
            actor=attention.WATCHDOG,
            ttl_seconds=600,
            now=MOMENT,
        )
        again = attention.claim(
            db,
            kind="stalled_agent",
            target="node-1",
            actor=attention.WATCHDOG,
            ttl_seconds=600,
            now=MOMENT + timedelta(seconds=30),
        )
        active = attention.active_claims(db, now=MOMENT + timedelta(seconds=30))

    assert again.allowed is True
    assert again.refreshed is True
    assert again.claim_id == first.claim_id
    assert "续期" in again.reason
    assert len(active) == 1, "续期不该产生第二条认领"


# ---------- T3 过期 ----------


def test_t3_expiry_frees_the_claim_for_anyone() -> None:
    """**过期的认领意味着"没人管了，谁都可以接手"** —— 与"还在处理"必须分得清。"""
    with SessionLocal() as db:
        attention.claim(
            db,
            kind="stalled_agent",
            target="node-1",
            actor=attention.WATCHDOG,
            ttl_seconds=60,
            now=MOMENT,
        )

    later = MOMENT + timedelta(days=365)
    with SessionLocal() as db:
        active = attention.active_claims(db, now=later)
        taken, _ = attention.is_claimed_by_other(
            db, kind="stalled_agent", target="node-1", actor=attention.STEWARD, now=later
        )
        takeover = attention.claim(
            db,
            kind="stalled_agent",
            target="node-1",
            actor=attention.STEWARD,
            reason="前一个认领过期了",
            now=later,
        )

    assert active == []
    assert taken is False
    assert takeover.allowed is True
    assert takeover.refreshed is False, "过期后的认领是**新**认领，不是续期"


def test_t3_expiry_boundary_is_exclusive() -> None:
    """恰好到期的那一刻算过期（`>` 而不是 `>=`）。

    边界写反的后果是认领多活一瞬间 —— 而"多活一瞬间"正是重复动作的窗口。
    """
    with SessionLocal() as db:
        row = attention.claim(
            db,
            kind="requeue",
            target="n",
            actor=attention.WATCHDOG,
            ttl_seconds=100,
            now=MOMENT,
        )
        assert row.claim_id is not None
        exactly = attention._active_row(db, "requeue", "n", MOMENT + timedelta(seconds=100))
        just_before = attention._active_row(db, "requeue", "n", MOMENT + timedelta(seconds=99))
    assert exactly is None
    assert just_before is not None


# ---------- T4 释放的权限 ----------


def test_t4_only_the_owner_can_release() -> None:
    """看门狗不该替管家宣布"管家的介入结束了"。"""
    claimed = _claim(actor=attention.STEWARD, target="node-9")
    with SessionLocal() as db:
        with pytest.raises(attention.ClaimError) as excinfo:
            attention.release(db, claimed.claim_id, actor=attention.WATCHDOG)
    assert "不能替它解除" in str(excinfo.value)
    # 被拒之后仍然生效。
    with SessionLocal() as db:
        assert len(attention.active_claims(db)) == 1


def test_t4_the_owner_can_release() -> None:
    claimed = _claim(actor=attention.STEWARD)
    with SessionLocal() as db:
        result = attention.release(db, claimed.claim_id, actor=attention.STEWARD)
        remaining = attention.active_claims(db)
    assert result["status"] == "released"
    assert result["forced"] is False
    assert remaining == []


def test_t4_forced_release_requires_a_signature() -> None:
    """人在界面上强制解除必须留名 —— 否则"谁把这条锁解开的"事后无从查起。"""
    claimed = _claim(actor=attention.STEWARD, target="node-9")
    with SessionLocal() as db:
        with pytest.raises(attention.ClaimError) as excinfo:
            attention.release(db, claimed.claim_id, actor=attention.HUMAN, force=True)
        assert "留名" in str(excinfo.value)

    with SessionLocal() as db:
        result = attention.release(
            db, claimed.claim_id, actor=attention.HUMAN, force=True, declared_by="duzuguang"
        )
        remaining = attention.active_claims(db)

    assert result["forced"] is True
    assert result["released_by"] == "duzuguang"
    assert "强制解除" in result["message"]
    assert remaining == []


def test_t4_releasing_a_missing_claim_is_an_error_not_a_crash() -> None:
    with SessionLocal() as db:
        with pytest.raises(attention.ClaimError):
            attention.release(db, 999999, actor=attention.WATCHDOG)


# ---------- T5 封闭词表 ----------


@pytest.mark.parametrize("kind", ["", "stalled", "STALLED_AGENT", "brand_new"])
def test_t5_unknown_kinds_are_refused(kind: str) -> None:
    with SessionLocal() as db:
        with pytest.raises(attention.ClaimError) as excinfo:
            attention.claim(db, kind=kind, target="n", actor=attention.WATCHDOG)
    assert "注意类型" in str(excinfo.value)


@pytest.mark.parametrize("actor", ["", "robot", "admin"])
def test_t5_unknown_actors_are_refused(actor: str) -> None:
    with SessionLocal() as db:
        with pytest.raises(attention.ClaimError) as excinfo:
            attention.claim(db, kind="requeue", target="n", actor=actor)
    assert "认领人" in str(excinfo.value)


def test_t5_actor_matching_is_case_insensitive_and_trimmed() -> None:
    allowed = _claim(actor="  WATCHDOG  ")
    assert allowed.allowed is True


# ---------- T6 时长 ----------


@pytest.mark.parametrize("ttl", [0, -1, -3600])
def test_t6_non_positive_ttl_is_refused(ttl: int) -> None:
    """一条不过期的认领会让看门狗永久闭眼。"""
    with SessionLocal() as db:
        with pytest.raises(attention.ClaimError) as excinfo:
            attention.claim(db, kind="requeue", target="n", actor=attention.WATCHDOG, ttl_seconds=ttl)
    assert "正数" in str(excinfo.value)


def test_t6_ttl_has_a_ceiling() -> None:
    """长达一年的"认领"实质上就是永久阻塞 —— 上限必须存在。"""
    with SessionLocal() as db:
        with pytest.raises(attention.ClaimError) as excinfo:
            attention.claim(
                db,
                kind="requeue",
                target="n",
                actor=attention.WATCHDOG,
                ttl_seconds=attention.MAX_TTL_SECONDS + 1,
            )
    assert "24 小时" in str(excinfo.value)

    with SessionLocal() as db:
        ok = attention.claim(
            db,
            kind="requeue",
            target="n",
            actor=attention.WATCHDOG,
            ttl_seconds=attention.MAX_TTL_SECONDS,
        )
    assert ok.allowed is True


def test_t6_ttl_must_be_an_integer() -> None:
    with SessionLocal() as db:
        with pytest.raises(attention.ClaimError):
            attention.claim(db, kind="requeue", target="n", actor=attention.WATCHDOG, ttl_seconds="很久")


# ---------- T7 给界面的形状 ----------


def test_t7_payload_carries_labels_and_remaining_seconds() -> None:
    _claim(ttl_seconds=600, reason="抢不到锁")
    with SessionLocal() as db:
        body = attention.payload(db)

    assert body["count"] == 1
    item = body["claims"][0]
    assert item["kind_label"] == "僵死进程"
    assert item["actor_label"].startswith("看门狗")
    assert 0 < item["remaining_seconds"] <= 600
    assert item["reason"] == "抢不到锁"
    # 词表也一起给出来（界面据此渲染筛选器），且不是自由字符串。
    assert {entry["kind"] for entry in body["kinds"]} == set(attention.KINDS)
    assert {entry["actor"] for entry in body["actors"]} == set(attention.ACTORS)
    assert "过期" in body["note"]


def test_t7_released_claims_do_not_show_up() -> None:
    claimed = _claim()
    with SessionLocal() as db:
        attention.release(db, claimed.claim_id, actor=attention.WATCHDOG)
        assert attention.payload(db)["count"] == 0


def test_t7_payload_counts_only_active_claims() -> None:
    with SessionLocal() as db:
        attention.claim(db, kind="stalled_agent", target="a", actor=attention.WATCHDOG, ttl_seconds=60, now=MOMENT)
        attention.claim(db, kind="requeue", target="b", actor=attention.STEWARD, ttl_seconds=60, now=MOMENT)
        now_count = attention.payload(db, now=MOMENT + timedelta(seconds=1))["count"]
        later = attention.payload(db, now=MOMENT + timedelta(days=365))["count"]
    assert now_count == 2
    assert later == 0
