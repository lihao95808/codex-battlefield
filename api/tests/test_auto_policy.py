"""自动策略的判据（P3-5）。

这个模块要解决的不是"能不能自动"，而是**自动得看不看得见**。所以判据分两层：

* **判定层**（纯函数）：该不该触发、依据是什么、关掉之后怎么说。
* **报告层**：每条策略的开关与"为什么没触发"必须都在报告里 ——
  用户要能分辨"这条策略没开"与"开着但没到条件"。**前者意味着他以为有保护其实没有。**

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| A1 | 关掉的策略**不触发**，且理由写明"已关闭" | 报告里看不出"没开"，用户以为有保护 |
| A2 | 连续失败达上限才停，且**必须在上限时触发** | 少一次是白烧钱，早一次是打断正常返工 |
| A3 | 收益递减只在**窗口内零成功**时降并发，且已在最低档时不再下调 | 加并发让失败更快更多；或把并发降到 0 = 静默停摆 |
| A4 | 空闲休眠要求**无活跃 + 无排队 + 过阈值**三条同时成立 | 有活在跑就休眠 = 打断长跑 |
| A5 | 僵死槽位只回收**有进程**的那些 | 进程都没了的归启动时清扫管，混在一起查不清是谁清的 |
| A6 | 拿不到数据时**不触发**（偏保守），并说明缺什么 | 猜一个值 → 误触发打断正常长跑 |
| A7 | 报告里**每条策略都在**，含开关状态与理由 | 只能看到"触发了什么"，看不到保护是否健在 |
| A8 | 设置上的开关**真的映射进观察字段** | "面板里关了、守护进程照旧"（最难归因的一类） |
| A9 | 动作词表封闭：策略只能声明词表里的动作 | 动作名漂移后没人知道谁在执行什么 |
"""
from __future__ import annotations

import pytest

from app import auto_policy
from app.config import Settings


# ---------- A1 关掉就不触发 ----------


@pytest.mark.parametrize(
    ("policy_id", "switch", "observation"),
    [
        (
            "stop_on_consecutive_failure",
            "auto_stop_on_consecutive_failure",
            {"consecutive_failures": {"n1": 99}},
        ),
        (
            "lower_concurrency_on_diminishing_returns",
            "auto_lower_concurrency_on_diminishing_returns",
            {"recent_results": ["failed"] * 20, "concurrency": 8, "auto_concurrency_min": 1},
        ),
        (
            "hibernate_when_idle",
            "auto_hibernate_when_idle",
            {"active_agents": 0, "queued_tasks": 0, "idle_seconds": 99999, "idle_hibernate_seconds": 60},
        ),
        (
            "reclaim_stale_slots",
            "auto_reclaim_stale_slots",
            {"slots": [{"task_id": "n1", "no_progress_seconds": 99999, "has_process": True}]},
        ),
    ],
)
def test_a1_a_disabled_policy_never_fires_and_says_so(
    policy_id: str, switch: str, observation: dict
) -> None:
    """关掉的策略不许触发，而且理由必须写成"已关闭"。

    这条防的是报告里看不出开关状态：用户以为有保护，其实那条策略根本没开。
    """
    observation = {**observation, switch: False}
    report = auto_policy.report(observation)
    item = next(entry for entry in report["policies"] if entry["id"] == policy_id)

    assert item["enabled"] is False
    assert item["fired"] is False
    assert "已关闭" in item["reason"]
    assert policy_id in report["disabled"]


def test_a7_report_lists_disabled_policies_separately() -> None:
    """"关掉的策略"要能一眼列出来 —— 那正是"以为有保护"的状态。"""
    report = auto_policy.report(
        {
            "auto_stop_on_consecutive_failure": False,
            "auto_hibernate_when_idle": False,
        }
    )
    assert set(report["disabled"]) == {"stop_on_consecutive_failure", "hibernate_when_idle"}
    assert report["fired_count"] == 0


# ---------- A2 连续失败 ----------


def test_a2_fires_exactly_at_the_limit() -> None:
    """上限前一次不触发、刚好等于上限触发 —— 边界是这里唯一容易写错的地方。"""
    base = {"auto_consecutive_failure_limit": 3}

    below = auto_policy.evaluate_consecutive_failure({**base, "consecutive_failures": {"n1": 2}})
    assert below.fired is False
    assert below.evidence["worst_streak"] == 2

    at = auto_policy.evaluate_consecutive_failure({**base, "consecutive_failures": {"n1": 3}})
    assert at.fired is True
    assert at.action == "stop_retrying"
    assert at.evidence["nodes"] == ["n1"]


def test_a2_limit_is_configurable_and_falls_back_to_the_builtin_default() -> None:
    """上限可配；配了非法值（0/负数）时回落到内置默认，而不是"永不触发"。"""
    fired_at_two = auto_policy.evaluate_consecutive_failure(
        {"auto_consecutive_failure_limit": 2, "consecutive_failures": {"n1": 2}}
    )
    assert fired_at_two.fired is True
    assert fired_at_two.evidence["limit"] == 2

    fallback = auto_policy.evaluate_consecutive_failure(
        {"auto_consecutive_failure_limit": 0, "consecutive_failures": {"n1": 3}}
    )
    assert fallback.evidence["limit"] == auto_policy.CONSECUTIVE_FAILURE_LIMIT


def test_a2_only_counts_nodes_that_can_still_run() -> None:
    """达上限才停 —— 一个节点失败 5 次当然要停，但只有 1 次的不能停。"""
    decision = auto_policy.evaluate_consecutive_failure(
        {"consecutive_failures": {"hot": 5, "cold": 1}}
    )
    assert decision.fired is True
    assert decision.evidence["nodes"] == ["hot"]
    assert "1 个节点" in decision.reason


# ---------- A3 收益递减 ----------


def test_a3_fires_only_when_a_full_window_has_zero_successes() -> None:
    window = auto_policy.DIMINISHING_WINDOW
    zeros = auto_policy.evaluate_diminishing_returns(
        {
            "recent_results": ["failed"] * window,
            "concurrency": 8,
            "auto_concurrency_min": 1,
        }
    )
    assert zeros.fired is True
    assert zeros.action == "lower_concurrency"
    assert zeros.evidence["successes"] == 0

    one_success = auto_policy.evaluate_diminishing_returns(
        {
            "recent_results": ["failed"] * (window - 1) + ["succeeded"],
            "concurrency": 8,
            "auto_concurrency_min": 1,
        }
    )
    assert one_success.fired is False
    assert "1 次成功" in one_success.reason


def test_a3_insufficient_history_does_not_fire() -> None:
    """历史不足一个窗口时**不触发** —— 2 次失败就降并发是误伤。"""
    decision = auto_policy.evaluate_diminishing_returns(
        {"recent_results": ["failed", "failed"], "concurrency": 8, "auto_concurrency_min": 1}
    )
    assert decision.fired is False
    assert "不足" in decision.reason


def test_a3_at_the_floor_it_stops_lowering() -> None:
    """已经在最低档时不再"降" —— 再降就是停摆，那是另一条策略（休眠）的事。"""
    decision = auto_policy.evaluate_diminishing_returns(
        {
            "recent_results": ["failed"] * auto_policy.DIMINISHING_WINDOW,
            "concurrency": 1,
            "auto_concurrency_min": 1,
        }
    )
    assert decision.fired is False
    assert "最低档" in decision.reason
    assert decision.action is None


# ---------- A4 空闲休眠 ----------


def test_a4_requires_no_active_no_queued_and_past_threshold() -> None:
    """三条必须同时成立。任一条不成立都不休眠。"""
    base = {"active_agents": 0, "queued_tasks": 0, "idle_seconds": 1000, "idle_hibernate_seconds": 600}

    assert auto_policy.evaluate_idle_hibernate(base).fired is True
    assert auto_policy.evaluate_idle_hibernate({**base, "active_agents": 1}).fired is False
    assert auto_policy.evaluate_idle_hibernate({**base, "queued_tasks": 1}).fired is False
    assert auto_policy.evaluate_idle_hibernate({**base, "idle_seconds": 599}).fired is False


def test_a4_boundary_at_the_threshold_fires() -> None:
    decision = auto_policy.evaluate_idle_hibernate(
        {"active_agents": 0, "queued_tasks": 0, "idle_seconds": 600, "idle_hibernate_seconds": 600}
    )
    assert decision.fired is True
    assert decision.action == "hibernate"


# ---------- A5 僵死槽位 ----------


def test_a5_reclaims_only_slots_that_still_have_a_process() -> None:
    """只回收"还有进程"的槽位；进程都没了的归启动时清扫管。

    两条通路混在一起时，"到底是谁清理的"在事故复盘里查不清。
    """
    stale_with_process = {
        "slots": [
            {"task_id": "alive-stuck", "no_progress_seconds": 99999, "has_process": True},
            {"task_id": "dead", "no_progress_seconds": 99999, "has_process": False},
        ]
    }
    decision = auto_policy.evaluate_stale_slots(stale_with_process)
    assert decision.fired is True
    assert decision.action == "reclaim_slots"
    assert decision.evidence["task_ids"] == ["alive-stuck"]


def test_a5_below_the_threshold_does_not_reclaim() -> None:
    """没到阈值不回收：长跑里几分钟没进度是常态，急着回收会打断真在干活的 agent。"""
    decision = auto_policy.evaluate_stale_slots(
        {"slots": [{"task_id": "n1", "no_progress_seconds": 10, "has_process": True}]}
    )
    assert decision.fired is False
    assert "没有僵死槽位" in decision.reason


def test_a5_slots_missing_fields_are_skipped_not_guessed() -> None:
    """槽位信息不全时跳过，不猜 `has_process=True`。"""
    decision = auto_policy.evaluate_stale_slots(
        {"slots": [{"task_id": "n1", "no_progress_seconds": 99999}]}
    )
    assert decision.fired is False


# ---------- A6 数据不足偏保守 ----------


@pytest.mark.parametrize(
    ("evaluate", "observation"),
    [
        (auto_policy.evaluate_consecutive_failure, {}),
        (auto_policy.evaluate_diminishing_returns, {}),
        (auto_policy.evaluate_idle_hibernate, {}),
        (auto_policy.evaluate_stale_slots, {}),
    ],
)
def test_a6_missing_data_never_fires_and_says_what_is_missing(evaluate, observation) -> None:
    """拿不到数据一律**不触发**，并说明缺什么。

    失效方向刻意偏保守：少触发一次最多是少省一点；误触发会打断一次正常的长跑。
    """
    decision = evaluate(observation)
    assert decision.fired is False
    assert decision.action is None
    assert decision.reason


# ---------- A8 设置真的接了进来 ----------


def test_a8_settings_switches_map_into_the_observation() -> None:
    """三个开关与上限必须**真的从 Settings 映射进观察字段**。

    这条防的是"面板里关了、守护进程照旧"：`Settings` 读到了值，但判定函数
    读的是别的键名 —— 那种错在界面上看起来完全正常。
    """
    settings = Settings(
        auto_stop_on_consecutive_failure=False,
        auto_hibernate_when_idle=False,
        auto_reclaim_stale_slots=False,
        auto_consecutive_failure_limit=7,
    )
    observation = auto_policy.observation_from_settings(settings)

    assert observation["auto_stop_on_consecutive_failure"] is False
    assert observation["auto_hibernate_when_idle"] is False
    assert observation["auto_reclaim_stale_slots"] is False
    assert observation["auto_consecutive_failure_limit"] == 7

    report = auto_policy.report(observation)
    assert set(report["disabled"]) == {
        "stop_on_consecutive_failure",
        "hibernate_when_idle",
        "reclaim_stale_slots",
    }


def test_a8_switches_are_editable_from_the_config_file() -> None:
    """三个开关都要登记在设置面板里，且 `Settings` 真的按 config key 读它们。

    这条与 P2-2 的 C1 是同一条规矩的另一半：登记了不可读 = 面板是摆设。
    """
    from app import settings_registry

    keys = {item.key for item in settings_registry.SETTINGS}
    for switch in (
        "auto_stop_on_consecutive_failure",
        "auto_consecutive_failure_limit",
        "auto_hibernate_when_idle",
        "auto_reclaim_stale_slots",
    ):
        assert switch in keys, f"{switch} 没有登记进设置面板"
    assert "policy" in dict(settings_registry.GROUPS), "自动策略没有自己的分组"


# ---------- A9 动作词表封闭 ----------


def test_a9_every_policy_declares_an_action_from_the_closed_vocabulary() -> None:
    """策略只能声明词表里的动作。

    动作名漂移（比如写成 `hibernate_tree`）之后，没人知道谁在执行什么 ——
    而调用方按名字分发时也不会报错，只是什么都不做。
    """
    for policy in auto_policy.POLICIES:
        assert policy.action in auto_policy.ACTIONS, f"{policy.id} 声明了词表外的动作"
        assert policy.id, "策略必须有 id"
        assert policy.label and policy.rationale, "策略必须带标签与理由"
        assert policy.switch, "策略必须指明它的开关名"


def test_a9_policy_ids_and_switches_are_unique() -> None:
    ids = [policy.id for policy in auto_policy.POLICIES]
    switches = [policy.switch for policy in auto_policy.POLICIES]
    assert len(set(ids)) == len(ids), f"策略 id 重复：{ids}"
    assert len(set(switches)) == len(switches), f"开关名重复：{switches}"


# ---------- A7 报告完整 ----------


def test_a7_report_always_contains_every_policy_with_a_reason() -> None:
    """报告里每条策略都在，且都有理由（触发与没触发都有）。"""
    report = auto_policy.report({})
    assert {item["id"] for item in report["policies"]} == {p.id for p in auto_policy.POLICIES}
    for item in report["policies"]:
        assert item["reason"], f"{item['id']} 没有理由"
        assert item["rationale"], f"{item['id']} 没有写明为什么存在"
        assert item["switch"], f"{item['id']} 没有开关名"


def test_a7_fired_list_matches_the_items() -> None:
    report = auto_policy.report({"consecutive_failures": {"n1": 99}})
    fired_items = [item["id"] for item in report["policies"] if item["fired"]]
    assert report["fired"] == fired_items
    assert report["fired_count"] == len(fired_items)
    assert "stop_on_consecutive_failure" in report["fired"]
