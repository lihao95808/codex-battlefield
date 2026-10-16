"""动作风险分级与预授权的判据（P4-4）。

这一层是**权限模型的实质性放宽**，所以判据几乎全是"拒绝"：
拒绝高风险被预授权、拒绝过期授权、拒绝越界、拒绝无名授权。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| AA1 | **高风险永不可预授权**，即使授权范围里写了它 | 一次"图省事"的授权就能全停所有项目 |
| AA2 | 默认**没有**授权：没记录就是每次都要人点 | 出厂即放宽权限 |
| AA3 | 动作集之外的动作被拒，且理由点名 | 笼统"未授权"让人无法判断该改哪里 |
| AA4 | 目标范围之外的目标被拒（含"不限目标"与"限定目标"的区别） | 授权针对 A 节点，却能让 B 节点被取消 |
| AA5 | 过期即拒（含恰好到期的边界） | 一次授权永久生效 |
| AA6 | 授权必须署名、必有正期限；**没有无限期形态** | 界面上"永久"和"24 小时"长得一样 |
| AA7 | 未分级的动作按 **high** 处理 | 新增动作静默获得预授权资格 |
| AA8 | `unclassified()` 能把"目录里有、没分级"的动作报出来 | 忘了分级没人知道，表现是"这个动作坏了" |
| AA9 | 放行时给出 `basis`，拒绝时给出**哪一条**不满足 | 审计无法回答"这次是谁批的" |
| AA10 | 高危动作的风险等级不许被改低（分级表与动作目录对得上） | 有人为了让某个动作能预授权而改分级 |
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import action_risk


def _grant(**over: object) -> action_risk.Grant:
    base: dict[str, object] = {
        "actions": ("kick_root", "retry_node"),
        "targets": ("node-a",),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "granted_by": "duzuguang",
        "reason": "长跑期间自动续跑",
    }
    base.update(over)
    return action_risk.Grant(**base)  # type: ignore[arg-type]


# ---------- AA1 高风险永不可预授权 ----------


@pytest.mark.parametrize("action", ["cancel_node", "cancel_all", "set_budget_guard"])
def test_aa1_high_risk_is_never_pre_authorizable_even_if_granted(action: str) -> None:
    """**这条是 P4-4 的核心取舍**：高风险动作即使被写进授权范围也不放行。

    做法是"人把危险动作加进授权列表"——那是图省事最常见的一步，
    而它看起来只是多加了一个字符串。
    """
    auth = action_risk.PreAuthorization(_grant(actions=(action,)))
    allowed, basis, reason = auth.authorize(action, "node-a")
    assert allowed is False
    assert basis == "human_confirmation_required"
    assert "高风险" in reason


def test_aa1_can_pre_authorize_is_a_constant_not_a_switch() -> None:
    """`can_pre_authorize` 不吃任何配置 —— 它只看风险等级。"""
    assert action_risk.can_pre_authorize("cancel_all") is False
    assert action_risk.can_pre_authorize("kick_root") is True
    # 未登记的动作也按高风险处理。
    assert action_risk.can_pre_authorize("brand_new_action") is False


# ---------- AA2 默认为空 ----------


def test_aa2_default_is_no_authorization() -> None:
    """出厂状态没有授权：每个动作都要人点。"""
    auth = action_risk.PreAuthorization()
    assert auth.grant is None
    allowed, basis, reason = auth.authorize("kick_root", "node-a")
    assert allowed is False
    assert basis == "human_confirmation_required"
    assert "没有预授权" in reason
    assert action_risk.default_grant() is None


def test_aa2_clear_removes_the_grant() -> None:
    """撤销之后立刻回到"要人确认" —— 撤销必须是彻底的。"""
    auth = action_risk.PreAuthorization(_grant())
    assert auth.authorize("kick_root", "node-a")[0] is True
    auth.clear()
    assert auth.authorize("kick_root", "node-a")[0] is False


# ---------- AA3 动作集 ----------


def test_aa3_action_outside_the_grant_is_refused_and_named() -> None:
    auth = action_risk.PreAuthorization(_grant(actions=("kick_root",)))
    allowed, _, reason = auth.authorize("retry_node", "node-a")
    assert allowed is False
    assert "retry_node" in reason
    assert "kick_root" in reason, "拒绝理由要点名授权集里到底有什么"


# ---------- AA4 目标范围 ----------


def test_aa4_target_outside_the_grant_is_refused() -> None:
    """授权针对 A 节点时不许让 B 节点被操作。"""
    auth = action_risk.PreAuthorization(_grant(targets=("node-a",)))
    allowed, _, reason = auth.authorize("kick_root", "node-b")
    assert allowed is False
    assert "node-b" in reason
    assert "node-a" in reason


def test_aa4_empty_targets_means_any_target_within_the_action_set() -> None:
    """"不限目标"是一个**显式**选择（空 target 集），与"限定目标"要能分辨。"""
    auth = action_risk.PreAuthorization(_grant(targets=()))
    assert auth.authorize("kick_root", "anything")[0] is True


# ---------- AA5 期限 ----------


def test_aa5_expired_grant_is_refused_including_the_boundary() -> None:
    """过期即拒。恰好到期的那一刻也算过期（`<=` 而不是 `<`）。

    边界写反的后果是"授权多活一瞬间"，而授权是不该有多余寿命的。
    """
    moment = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    auth = action_risk.PreAuthorization(_grant(expires_at=moment))

    allowed, _, reason = auth.authorize("kick_root", "node-a", now=moment)
    assert allowed is False
    assert "过期" in reason

    just_before = moment - timedelta(seconds=1)
    assert auth.authorize("kick_root", "node-a", now=just_before)[0] is True


def test_aa5_remaining_seconds_is_reported() -> None:
    moment = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    auth = action_risk.PreAuthorization(_grant(expires_at=moment + timedelta(minutes=5)))
    allowed, _, reason = auth.authorize("kick_root", "node-a", now=moment)
    assert allowed is True
    assert "300s" in reason


# ---------- AA6 造授权时的必填项 ----------


def test_aa6_make_grant_requires_a_positive_window() -> None:
    """**没有无限期的预授权。**"""
    for minutes in (0, -1):
        with pytest.raises(action_risk.RiskError) as excinfo:
            action_risk.make_grant(
                actions=["kick_root"], minutes=minutes, granted_by="me", reason="r"
            )
        assert "期限" in str(excinfo.value)


def test_aa6_make_grant_requires_a_signature() -> None:
    """必须署名 —— 否则事后无法回答"这条授权是谁给的"。"""
    with pytest.raises(action_risk.RiskError) as excinfo:
        action_risk.make_grant(actions=["kick_root"], minutes=5, granted_by="  ", reason="r")
    assert "署名" in str(excinfo.value)


def test_aa6_make_grant_rejects_high_risk_actions() -> None:
    """在造授权这一步就挡住高危动作，而不是等执行时才拒。

    早挡的价值：用户不会以为自己已经批过了。
    """
    with pytest.raises(action_risk.RiskError) as excinfo:
        action_risk.make_grant(
            actions=["kick_root", "cancel_all"], minutes=5, granted_by="me", reason="r"
        )
    message = str(excinfo.value)
    assert "cancel_all" in message
    assert "高风险" in message


def test_aa6_make_grant_rejects_unknown_actions_and_empty_lists() -> None:
    with pytest.raises(action_risk.RiskError):
        action_risk.make_grant(actions=["not_an_action"], minutes=5, granted_by="me", reason="r")
    with pytest.raises(action_risk.RiskError):
        action_risk.make_grant(actions=[], minutes=5, granted_by="me", reason="r")


def test_aa6_make_grant_dedupes_and_widens_the_window() -> None:
    grant = action_risk.make_grant(
        actions=["retry_node", "kick_root", "retry_node"],
        targets=["b", "a", "a"],
        minutes=10,
        granted_by=" me ",
        reason=" r ",
    )
    assert grant.actions == ("kick_root", "retry_node")
    assert grant.targets == ("a", "b")
    assert grant.granted_by == "me"
    assert 0 < grant.remaining_seconds() <= 600


# ---------- AA7 未分级一律按高风险 ----------


def test_aa7_unclassified_actions_default_to_high() -> None:
    """漏登记的动作按 high 处理 —— 默认值必须偏保守。"""
    assert action_risk.risk_of("some_new_action") == action_risk.HIGH
    assert action_risk.risk_of("cancel_all") == action_risk.HIGH


def test_aa7_every_level_has_a_label_and_a_rationale() -> None:
    """三档都要有中文说明 —— 光写"高风险"不解释为什么。"""
    for level in action_risk.RISK_LEVELS:
        assert action_risk.RISK_LABEL[level]
        assert action_risk.RISK_RATIONALE[level]
    assert set(action_risk.RISK_LEVELS) == set(action_risk.RISK_LABEL)


# ---------- AA8 漏分级要能被发现 ----------


def test_aa8_unclassified_reports_actions_missing_from_the_table() -> None:
    missing = action_risk.unclassified(["kick_root", "cancel_all", "brand_new"])
    assert missing == ["brand_new"]


def test_aa8_the_real_action_catalog_is_fully_classified() -> None:
    """**接线判据**：管家能提议的每个动作都必须在风险表里。

    这条挂的是真实目录（`steward_actions.known_actions()`），不是一份写死的清单 ——
    所以新增动作时忘了分级，这里会红。
    """
    from app.steward_actions import known_actions

    names = list(known_actions())
    assert names, "动作目录是空的 —— 这条判据会空转"
    assert action_risk.unclassified(names) == [], "有动作没有风险分级"


def test_aa8_every_action_has_an_executor() -> None:
    """风险表与执行器表必须一一对应：分级了却没有执行器 = 提议出来必失败。"""
    from app.steward_actions import EXECUTORS

    assert set(action_risk.RISK_BY_ACTION) == set(EXECUTORS), (
        "风险分级表与执行器表不一致"
    )


# ---------- AA9 审计依据 ----------


def test_aa9_allowance_reports_the_basis() -> None:
    """放行时给 `pre_authorized` 与署名 —— 审计要能回答"这次是谁批的"。"""
    auth = action_risk.PreAuthorization(_grant())
    allowed, basis, reason = auth.authorize("kick_root", "node-a")
    assert allowed is True
    assert basis == "pre_authorized"
    assert "duzuguang" in reason


def test_aa9_every_refusal_names_the_rule_that_failed() -> None:
    """四种拒绝各有各的说法，不能都是"未授权"。

    ⚠️ 时间基准必须是**当下**而不是一个写死的时刻：判定里"过期"排在
    "动作集/目标"之前，用一个写死的历史时刻会让四条都落到"已过期"上 ——
    于是这条判据看着通过，其实一条规则都没验（第一版就是这么写错的）。
    """
    moment = datetime.now(timezone.utc)
    cases = {
        "风险等级": (action_risk.PreAuthorization(_grant(actions=("cancel_all",))), "cancel_all", "node-a"),
        "没有预授权": (action_risk.PreAuthorization(), "kick_root", "node-a"),
        "动作集": (action_risk.PreAuthorization(_grant(actions=("kick_root",))), "retry_node", "node-a"),
        "目标": (action_risk.PreAuthorization(_grant(targets=("node-a",))), "kick_root", "node-z"),
    }
    reasons: dict[str, str] = {}
    for label, (auth, action, target) in cases.items():
        allowed, basis, reason = auth.authorize(action, target, now=moment)
        assert allowed is False, f"{label} 应当被拒"
        assert basis == "human_confirmation_required"
        assert "过期" not in reason, f"{label} 落到了过期分支 —— 这条判据没验到它该验的规则"
        reasons[label] = reason

    # 过期单独一条（要指定时间）
    expired = action_risk.PreAuthorization(_grant(expires_at=moment - timedelta(seconds=1)))
    allowed, _, reason = expired.authorize("kick_root", "node-a", now=moment)
    assert allowed is False and "过期" in reason

    assert len(set(reasons.values())) == len(reasons), "拒绝理由重复了 —— 说明没有说清哪一条不满足"


# ---------- AA10 分级本身不许被悄悄改低 ----------


def test_aa10_the_dangerous_actions_stay_high() -> None:
    """把不可逆/全局动作改成低风险必须让测试红。

    这条是"有人为了让某动作能预授权而改分级"的防线。
    """
    for action in ("cancel_node", "cancel_all", "set_budget_guard"):
        assert action_risk.risk_of(action) == action_risk.HIGH, (
            f"{action} 的风险等级被改低了 —— 它不可逆或影响全局"
        )


def test_aa10_catalog_marks_high_risk_as_not_pre_authorizable() -> None:
    items = action_risk.catalog()
    highs = [item for item in items if item["risk"] == action_risk.HIGH]
    assert highs, "目录里没有高风险动作 —— 分级表可能被清空了"
    assert all(item["pre_authorizable"] is False for item in highs)
    # 排序：危险的在前面（先被看见）。
    assert items[0]["risk"] == action_risk.HIGH


def test_aa10_catalog_covers_every_known_action() -> None:
    from app.steward_actions import known_actions

    names = sorted(known_actions())
    assert [item["action"] for item in action_risk.catalog()] == sorted(
        names, key=lambda name: ({action_risk.HIGH: 0, action_risk.MEDIUM: 1, action_risk.LOW: 2}[action_risk.risk_of(name)], name)
    )
