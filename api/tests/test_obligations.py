"""债务表（obligations）纯模块的单元测试。

**本文件只覆盖模块层**：幂等键、severity 分级、清债留痕规则、汇总恒等式、版本债务的方向敏感、
轮次域清债的有界条件。

**接线测试在别处**：`tests/test_obligation_gate.py`。把债务表接进 `_accept_parent` 的第一版
实现引入了**自增强循环**——门控在收尾处创建"修复子任务"，而修复子任务自身又成为新的产出方，
于是下一轮集成生成新的冲突、再建子任务，子任务与债务一起无限增长。实测表现是测试挂死
（单个用例跑 1h40m）+ `RecursionError: maximum recursion depth exceeded`（SQLAlchemy 的
Select 因 task 集合膨胀而在 cache_key 递归爆栈）。该接线已回退并按下面两条硬约束重做：

1. **门控不得创建子任务**——只在收尾处拒绝标记 done，把裁决交回 agent 或人；
2. **清债必须有界且可判**——用"本轮集成不再复现该冲突"（`settle_round_cleared`）而不是
   "修复子任务完成"；后者在"子任务完成但冲突仍在"时会让门永远关着。
"""

from __future__ import annotations

import pytest

from app import obligations


def test_obligation_id_is_stable_for_same_conflict() -> None:
    first = obligations.obligation_id(obligations.INTEGRATION_CONFLICT, "parent-1", "src/x.gd")
    second = obligations.obligation_id(obligations.INTEGRATION_CONFLICT, "parent-1", "src/x.gd")
    assert first == second
    assert first != obligations.obligation_id(obligations.INTEGRATION_CONFLICT, "parent-1", "src/y.gd")


def test_obligation_id_is_bound_to_the_parent_not_the_writer() -> None:
    """幂等键绑在"这一件事"（父节点 + 路径）上，不绑在"谁产出的"上。

    这不是风格问题：若把产出方写进 id，为这条债创建的修复子任务自身会成为新的产出方，
    下一轮会生成一个全新 id，于是债务与子任务一起无限增长（第一版接线的实测故障）。
    """
    incoming = obligations.obligations_from_integration(
        "parent-1",
        {"conflicts": [{"type": "path_collision", "path": "src/x.gd", "kept": "n2", "overwritten": ["n1"]}]},
    )
    assert incoming[0]["source_node_id"] == "parent-1"
    assert incoming[0]["id"] == "integration_conflict:parent-1:src/x.gd"
    # 产出方信息保留在 payload 里供裁决用，但**不参与身份**：
    # 换一批产出方后 id 必须不变，否则每轮集成都会造出新债。
    shifted = obligations.obligations_from_integration(
        "parent-1",
        {"conflicts": [{"type": "path_collision", "path": "src/x.gd", "kept": "n9", "overwritten": ["n1", "n2"]}]},
    )
    assert shifted[0]["id"] == incoming[0]["id"]


def test_upsert_is_idempotent_for_unsettled() -> None:
    incoming = obligations.obligations_from_integration(
        "p",
        {"conflicts": [{"type": "path_collision", "path": "src/x.gd", "kept": "n1", "overwritten": ["n2"]}]},
    )
    once = obligations.upsert([], incoming)
    twice = obligations.upsert(once, incoming)
    assert len(once) == 1
    # 同一冲突在多次集成里被重复识别，不得重复记债
    assert len(twice) == 1
    assert twice[0]["recurrence_count"] == 0


def test_writer_change_does_not_create_a_new_obligation() -> None:
    """同一路径在不同轮次换了产出方，仍是同一条债。"""
    first = obligations.upsert(
        [],
        obligations.obligations_from_integration("p", {"conflicts": [{"path": "a", "kept": "n1", "overwritten": ["n2"]}]}),
    )
    second = obligations.upsert(
        first,
        obligations.obligations_from_integration("p", {"conflicts": [{"path": "a", "kept": "n9", "overwritten": ["n1", "n2"]}]}),
    )
    assert len(second) == 1


def test_upsert_does_not_resurrect_settled_obligation() -> None:
    incoming = obligations.obligations_from_integration(
        "p",
        {"conflicts": [{"type": "path_collision", "path": "src/x.gd", "kept": "n1", "overwritten": ["n2"]}]},
    )
    items = obligations.upsert([], incoming)
    target = items[0]["id"]
    items = obligations.settle(items, target, settled_by=obligations.SETTLED_BY_REPAIR_CHILD, reason="fixed")
    again = obligations.upsert(items, incoming)
    # 不复活是刻意的：否则"清掉→下一轮又报同一条"会让人反复清债，形成死循环
    assert again[0]["settled_at"] is not None
    assert again[0]["recurrence_count"] == 1


def test_human_settle_requires_reason() -> None:
    items = obligations.upsert(
        [], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a", "kept": "n1"}]})
    )
    with pytest.raises(ValueError):
        obligations.settle(items, items[0]["id"], settled_by=obligations.SETTLED_BY_HUMAN, reason="   ")
    # 机器通路不强制 reason：它们各有可核对的依据
    settled = obligations.settle(items, items[0]["id"], settled_by=obligations.SETTLED_BY_VERSION_CATCHUP)
    assert settled[0]["settled_at"] is not None


def test_settling_an_already_settled_obligation_is_rejected() -> None:
    items = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    items = obligations.settle(items, items[0]["id"], settled_by=obligations.SETTLED_BY_REPAIR_CHILD)
    with pytest.raises(KeyError):
        obligations.settle(items, items[0]["id"], settled_by=obligations.SETTLED_BY_REPAIR_CHILD)


def test_unknown_settle_path_is_rejected() -> None:
    items = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    with pytest.raises(ValueError):
        obligations.settle(items, items[0]["id"], settled_by="magic")


# --- 人清债：放宽不是禁止的，**无声的**放宽才是（§8.2） ----------------------


def test_human_settlement_records_reason_and_actor() -> None:
    items = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    settled = obligations.settle(
        items, items[0]["id"], settled_by=obligations.SETTLED_BY_HUMAN, reason="已知无害", actor="operator"
    )
    assert settled[0]["settled_by"] == "human"
    assert settled[0]["settle_reason"] == "已知无害"
    assert settled[0]["settled_actor"] == "operator"
    assert obligations.summarize(settled)["settled_by_human_count"] == 1


def test_human_settlement_without_reason_or_actor_is_rejected() -> None:
    items = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    with pytest.raises(ValueError):
        obligations.settle(items, items[0]["id"], settled_by=obligations.SETTLED_BY_HUMAN, actor="operator")
    with pytest.raises(ValueError):
        obligations.settle(items, items[0]["id"], settled_by=obligations.SETTLED_BY_HUMAN, reason="已知无害")
    with pytest.raises(ValueError):
        obligations.settle(items, items[0]["id"], settled_by=obligations.SETTLED_BY_HUMAN, reason="  ", actor="  ")


def test_machine_settlement_does_not_require_a_human_reason() -> None:
    """机器通路各自有可核对的依据（修复子任务完成 / 版本追平 / 本轮不再复现），不强制 reason。"""
    items = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    for path in (obligations.SETTLED_BY_REPAIR_CHILD, obligations.SETTLED_BY_VERSION_CATCHUP, obligations.SETTLED_BY_ROUND_CLEAR):
        settled = obligations.settle(items, items[0]["id"], settled_by=path)
        assert settled[0]["settled_by"] == path


def test_skipped_severity_rules():
    # 写法问题不阻断（与档位无关）
    assert obligations.skipped_severity("absolute path") == obligations.SEVERITY_ADVISORY
    # 越界是边界契约被破坏，与档位无关一律阻断
    assert obligations.skipped_severity("outside integration boundary", "overwrite") == obligations.SEVERITY_BLOCKING
    assert obligations.skipped_severity("outside integration boundary", "keep_both") == obligations.SEVERITY_BLOCKING
    # 声明了却没产出属"产物完整性"，与冲突同类，由档位决定
    assert obligations.skipped_severity("source missing", "repair") == obligations.SEVERITY_BLOCKING
    assert obligations.skipped_severity("source missing", "overwrite") == obligations.SEVERITY_ADVISORY
    assert obligations.skipped_severity("source missing") == obligations.SEVERITY_ADVISORY


def test_source_missing_under_the_default_strategy_does_not_hard_block() -> None:
    """这一条是修正过的写法：原文一律 blocking，实测把默认档悄悄变成了阻断档。"""
    items = obligations.obligations_from_integration(
        "p", {"skipped": [{"path": "a.txt", "reason": "source missing"}]}, strategy="overwrite"
    )
    assert obligations.blocking(items) == []
    strict = obligations.obligations_from_integration(
        "p", {"skipped": [{"path": "a.txt", "reason": "source missing"}]}, strategy="repair"
    )
    assert obligations.has_blocking(strict) is True


def test_stale_severity_is_direction_sensitive() -> None:
    assert obligations.stale_severity("tighten") == obligations.SEVERITY_BLOCKING
    assert obligations.stale_severity("mixed") == obligations.SEVERITY_BLOCKING
    # 放宽只上浮不阻断：产物仍合规，且不得追溯洗白已失败节点
    assert obligations.stale_severity("loosen") == obligations.SEVERITY_ADVISORY


def test_version_obligation_only_when_behind() -> None:
    assert obligations.obligations_from_version(node_id="n", node_version=2, effective_version=2) == []
    assert obligations.obligations_from_version(node_id="n", node_version=3, effective_version=2) == []
    behind = obligations.obligations_from_version(node_id="n", node_version=1, effective_version=2, depth=3)
    assert len(behind) == 1
    # 深度必须记下来：约束/版本漂移随委托链累积，事后要能定位是第几跳引入的
    assert behind[0]["depth"] == 3
    assert behind[0]["severity"] == obligations.SEVERITY_BLOCKING


def test_blocking_filters_settled_and_advisory() -> None:
    items = obligations.obligations_from_integration(
        "p",
        {
            "conflicts": [{"path": "a", "kept": "n1"}],
            "skipped": [{"path": "b", "reason": "absolute path"}, {"path": "c", "reason": "source missing"}],
        },
        strategy="repair",
    )
    assert len(obligations.blocking(items)) == 2
    advisory = [item for item in items if item["severity"] == obligations.SEVERITY_ADVISORY]
    assert len(advisory) == 1
    items = obligations.settle(items, obligations.blocking(items)[0]["id"], settled_by=obligations.SETTLED_BY_REPAIR_CHILD)
    assert len(obligations.blocking(items)) == 1


def test_summary_counts_and_identity() -> None:
    items = obligations.obligations_from_integration(
        "p",
        {
            "conflicts": [{"path": "a", "kept": "n1"}],
            "skipped": [{"path": "b", "reason": "absolute path"}, {"path": "c", "reason": "source missing"}],
        },
        strategy="repair",
    )
    summary = obligations.summarize(items)
    assert summary["unsettled_total"] == 3
    assert summary["blocking_total"] == 2
    assert summary["advisory_total"] == 1
    # 恒等式：分类求和必须等于总数，否则看板会自相矛盾
    assert summary["blocking_total"] + summary["advisory_total"] == summary["unsettled_total"]
    assert sum(summary["by_type"].values()) == summary["unsettled_total"]


# --- 冲突严重度由**策略**决定（§6.1 判据 2 是一条无条件判据） ----------------


def test_conflict_severity_follows_the_integration_strategy() -> None:
    """严重度必须是债务的属性，不是门控开关的属性。

    否则看板会出现"报阻断但树照跑"——§9 的用途（一眼看出树有没有被卡）当场失效。
    """
    assert obligations.conflict_severity("repair") == obligations.SEVERITY_BLOCKING
    assert obligations.conflict_severity("overwrite") == obligations.SEVERITY_ADVISORY
    assert obligations.conflict_severity("keep_both") == obligations.SEVERITY_ADVISORY
    # 漏传策略时不得声称阻断：那会把默认档悄悄变成阻断档
    assert obligations.conflict_severity("") == obligations.SEVERITY_ADVISORY
    assert obligations.conflict_severity(None) == obligations.SEVERITY_ADVISORY


def test_conflict_obligations_carry_the_strategy_they_were_judged_under() -> None:
    items = obligations.obligations_from_integration("p", {"conflicts": [{"path": "src/a.gd"}]}, strategy="repair")
    assert items[0]["severity"] == obligations.SEVERITY_BLOCKING
    assert items[0]["payload"]["strategy"] == "repair"
    loose = obligations.obligations_from_integration("p", {"conflicts": [{"path": "src/a.gd"}]}, strategy="overwrite")
    assert loose[0]["severity"] == obligations.SEVERITY_ADVISORY
    assert obligations.blocking(loose) == []


def test_integration_conflict_is_not_an_always_blocking_type() -> None:
    """写死成"天然阻断"再让门控按配置过滤，正是"看板与实际不一致"的成因。"""
    assert obligations.INTEGRATION_CONFLICT not in obligations.ALWAYS_BLOCKING


def test_skipped_severity_stays_strategy_independent() -> None:
    """越界与"声明了却没产出"是真丢了东西，不因为档位宽松就无害。"""
    for strategy in ("overwrite", "keep_both", "repair", ""):
        items = obligations.obligations_from_integration(
            "p", {"skipped": [{"path": "b", "reason": "outside integration boundary"}]}, strategy=strategy
        )
        assert items[0]["severity"] == obligations.SEVERITY_BLOCKING


def test_summary_exposes_human_settlements_as_a_measure() -> None:
    """`settled_by_human_count` 是"被人手动放宽了多少次"的可见计量。"""
    items = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    items = obligations.settle(
        items, items[0]["id"], settled_by=obligations.SETTLED_BY_HUMAN, reason="已知无害", actor="operator"
    )
    summary = obligations.summarize(items)
    assert summary["settled_by_human_count"] == 1
    assert summary["settled_total"] == 1
    assert summary["unsettled_total"] == 0


def test_summary_reports_depth_distribution() -> None:
    items = obligations.obligations_from_version(node_id="n", node_version=1, effective_version=2, depth=4)
    summary = obligations.summarize(items)
    assert summary["by_depth"] == {"4": 1}


# --- 轮次域清债：§6.4 硬约束 2 的"有界且可判" -------------------------------


def test_round_clear_settles_what_this_round_no_longer_reproduces() -> None:
    existing = obligations.upsert(
        [],
        obligations.obligations_from_integration(
            "p", {"conflicts": [{"path": "src/x.gd"}, {"path": "src/y.gd"}]}
        ),
    )
    incoming = obligations.obligations_from_integration("p", {"conflicts": [{"path": "src/x.gd"}]})
    cleared = obligations.settle_round_cleared(existing, incoming)
    by_id = {item["id"]: item for item in cleared}
    assert by_id["integration_conflict:p:src/y.gd"]["settled_by"] == obligations.SETTLED_BY_ROUND_CLEAR
    assert by_id["integration_conflict:p:src/y.gd"]["settle_reason"]
    # 本轮仍复现的那条**不得**被清
    assert by_id["integration_conflict:p:src/x.gd"]["settled_at"] is None


def test_round_clear_never_touches_version_debt() -> None:
    """版本类债务不由集成轮次定义。若允许"这轮没提到它"就清掉，那就是静默放宽。"""
    version_debt = obligations.obligations_from_version(node_id="n", node_version=1, effective_version=2)
    conflict = obligations.obligations_from_integration("p", {"conflicts": [{"path": "src/x.gd"}]})
    cleared = obligations.settle_round_cleared(version_debt + conflict, [])
    by_type = {item["type"]: item for item in cleared}
    # 冲突：本轮不再复现 → 清；版本债：与轮次无关 → 一动不动
    assert by_type[obligations.INTEGRATION_CONFLICT]["settled_by"] == obligations.SETTLED_BY_ROUND_CLEAR
    assert by_type[obligations.CONSTITUTION_STALE]["settled_at"] is None
    assert by_type[obligations.CONSTITUTION_STALE]["settled_by"] is None


def test_round_clear_is_idempotent_and_does_not_resurrect_settled_items() -> None:
    existing = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    once = obligations.settle_round_cleared(existing, [])
    twice = obligations.settle_round_cleared(once, [])
    assert [item["id"] for item in once] == [item["id"] for item in twice]
    assert once[0]["settled_at"] == twice[0]["settled_at"]
    # 同一件事下一轮又复现时，已清项只累加复发计数，不复活（否则人会被迫反复清债）
    revived = obligations.upsert(twice, obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    assert len(revived) == 1
    assert revived[0]["settled_at"] is not None
    assert revived[0]["recurrence_count"] == 1
    assert obligations.blocking(revived) == []


def test_round_clear_settle_path_is_a_named_path_not_a_free_string() -> None:
    items = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    settled = obligations.settle(items, items[0]["id"], settled_by=obligations.SETTLED_BY_ROUND_CLEAR)
    assert settled[0]["settled_by"] == "round_clear"
    assert obligations.SETTLED_BY_ROUND_CLEAR in obligations.VALID_SETTLED_BY


# --- 上浮事实（§7.2.3） ----------------------------------------------------


def test_upward_fact_is_one_line_per_branch_not_a_copy() -> None:
    """不是把子树那一串债复制上来：复制的后果是全树翻倍，看板反而更难看。"""
    fact = obligations.upward_blocking_debt("parent", "child", blocking_total=7, by_type={"a": 4, "b": 3}, depth=1)
    assert fact is not None
    assert fact["id"] == "child_blocked:parent:child"
    assert fact["type"] == obligations.CHILD_BLOCKED
    assert fact["severity"] == obligations.SEVERITY_BLOCKING
    assert fact["payload"]["blocking_total"] == 7
    assert fact["payload"]["by_type"] == {"a": 4, "b": 3}
    assert fact["depth"] == 1


def test_upward_fact_is_absent_when_there_is_nothing_blocking() -> None:
    assert obligations.upward_blocking_debt("parent", "child", blocking_total=0) is None


def test_upward_fact_id_is_stable_regardless_of_how_many_debts_there_are() -> None:
    """幂等键是 (父, 子)：同一支上无论多少条债，父节点上永远只有一条。"""
    one = obligations.upward_blocking_debt("p", "c", blocking_total=1)
    many = obligations.upward_blocking_debt("p", "c", blocking_total=9)
    assert one is not None and many is not None
    assert one["id"] == many["id"]


def test_upward_fact_clears_when_the_branch_is_no_longer_blocked() -> None:
    existing = obligations.upsert(
        [],
        [
            obligations.upward_blocking_debt("p", "c1", blocking_total=1),
            obligations.upward_blocking_debt("p", "c2", blocking_total=2),
        ],
    )
    cleared = obligations.settle_upward_cleared(existing, still_blocked_child_ids={"c2"})
    by_id = {item["id"]: item for item in cleared}
    assert by_id["child_blocked:p:c1"]["settled_by"] == obligations.SETTLED_BY_UPWARD_CLEAR
    assert by_id["child_blocked:p:c2"]["settled_at"] is None
    assert obligations.summarize(cleared)["blocking_total"] == 1


def test_upward_fact_survives_repeated_refresh_without_duplication() -> None:
    stored = obligations.upsert([], [obligations.upward_blocking_debt("p", "c", blocking_total=1)])
    for _ in range(3):
        incoming = [obligations.upward_blocking_debt("p", "c", blocking_total=1)]
        stored = obligations.upsert(obligations.settle_upward_cleared(stored, still_blocked_child_ids={"c"}), incoming)
    assert len(stored) == 1


# --- §7.3 子节点阻塞/失败债：唯一真相，报告由它派生 ------------------------


def test_child_status_debt_covers_blocked_and_failed_only() -> None:
    blocked = obligations.child_status_debt("p", "c", status="blocked", report={"retryable": True})
    failed = obligations.child_status_debt("p", "c", status="failed", report={})
    assert blocked is not None and failed is not None
    assert blocked["type"] == obligations.CHILD_BLOCKED
    assert failed["type"] == obligations.CHILD_FAILED
    assert blocked["id"] == "child_blocked:p:c"
    assert blocked["payload"]["report"] == {"retryable": True}
    assert obligations.child_status_debt("p", "c", status="done") is None
    assert obligations.child_status_debt("p", "c", status="running") is None


def test_blocked_child_reports_are_derived_from_unsettled_debt() -> None:
    """两套真相是最容易出的一类错：报告还在、债已经清了（或者反过来）。"""
    items = obligations.upsert(
        [],
        [
            obligations.child_status_debt("p", "c1", status="blocked", report={"task_id": "c1"}),
            obligations.child_status_debt("p", "c2", status="failed", report={"task_id": "c2"}),
        ],
    )
    assert [report["task_id"] for report in obligations.child_status_reports(items)] == ["c1", "c2"]

    settled = obligations.settle_child_status_cleared(items, still_blocking_child_ids={"c1"})
    assert [report["task_id"] for report in obligations.child_status_reports(settled)] == ["c1"]
    assert [item["settled_by"] for item in settled] == [None, "repair_child"]


def test_repaired_child_clears_the_debt_through_the_repair_path() -> None:
    """§6.3 的第一条通路：修复子任务完成 → 债被 `repair_child` 清掉。"""
    items = obligations.upsert([], [obligations.child_status_debt("p", "c1", status="blocked", report={"task_id": "c1"})])
    cleared = obligations.settle_child_status_cleared(items, still_blocking_child_ids=set())
    assert cleared[0]["settled_by"] == obligations.SETTLED_BY_REPAIR_CHILD
    assert obligations.summarize(cleared)["blocking_total"] == 0
    assert obligations.child_status_reports(cleared) == []


def test_child_status_debt_is_idempotent_per_child() -> None:
    first = obligations.upsert([], [obligations.child_status_debt("p", "c1", status="blocked", report={"task_id": "c1"})])
    again = obligations.upsert(
        first, [obligations.child_status_debt("p", "c1", status="blocked", report={"task_id": "c1", "category": "x"})]
    )
    assert len(again) == 1
    assert again[0]["payload"]["report"]["category"] == "x"


# --- 看板汇总（§9 / O8） ----------------------------------------------------


def _board_fixture() -> list[dict]:
    conflict = obligations.obligations_from_integration(
        "p", {"conflicts": [{"path": "src/a.gd"}]}, strategy="repair"
    )
    stale = obligations.obligations_from_version(node_id="n1", node_version=1, effective_version=2, depth=2)
    revalidate = [
        obligations.make_obligation(
            obligations.REVALIDATE_REQUIRED, source_node_id="n1", discriminator="v1->v2", depth=2
        )
    ]
    advisory = obligations.obligations_from_integration(
        "p", {"skipped": [{"reason": "absolute path", "path": "/abs/x.gd"}]}
    )
    return obligations.upsert([], [*conflict, *stale, *revalidate, *advisory])


def test_board_identity_by_type_sums_to_the_totals() -> None:
    """O8：`by_type` 求和必须等于 blocking + advisory——否则条带上的数字会自己打自己。"""
    summary = obligations.summarize(_board_fixture())
    assert sum(summary["by_type"].values()) == summary["blocking_total"] + summary["advisory_total"]
    assert summary["unsettled_total"] == summary["blocking_total"] + summary["advisory_total"]
    assert summary["blocking_total"] == 3  # 冲突 + 落后 + 待再验证
    assert summary["advisory_total"] == 1  # absolute path 只是写法问题


def test_board_reports_the_revalidate_queue_depth() -> None:
    assert obligations.summarize(_board_fixture())["revalidate_queue_depth"] == 1


def test_board_reports_how_long_the_oldest_blocker_has_been_stuck() -> None:
    """区分"在干活"与"卡死"的唯一手段。没有它，挂了三天和刚记上长得一样。"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    items = obligations.upsert(
        [], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}, strategy="repair")
    )
    items[0]["created_at"] = (now - timedelta(hours=3)).isoformat()
    summary = obligations.summarize(items, now=now)
    assert 10_700 < summary["oldest_blocking_age_seconds"] < 10_900


def test_oldest_blocking_age_is_none_when_no_timestamp_is_judgeable() -> None:
    """判不出来就说判不出来，**不许**用 0 冒充"刚记上"。"""
    items = obligations.upsert([], obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}))
    items[0].pop("created_at", None)
    assert obligations.summarize(items)["oldest_blocking_age_seconds"] is None
    items[0]["created_at"] = "不是时间"
    assert obligations.summarize(items)["oldest_blocking_age_seconds"] is None


def test_oldest_age_ignores_advisory_and_settled_items() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    advisory = obligations.obligations_from_integration("p", {"skipped": [{"reason": "absolute path", "path": "/abs/a"}]})
    advisory[0]["created_at"] = (now - timedelta(days=9)).isoformat()
    blocking_item = obligations.obligations_from_integration("p", {"conflicts": [{"path": "a"}]}, strategy="repair")
    blocking_item[0]["created_at"] = (now - timedelta(minutes=5)).isoformat()
    summary = obligations.summarize(advisory + blocking_item, now=now)
    assert 290 < summary["oldest_blocking_age_seconds"] < 310


def test_tree_level_merge_takes_the_oldest_blocker_not_the_average() -> None:
    """树卡了多久由**最老**的那条决定；取平均或取最近一条会把"卡死"读成"在干活"。"""
    merged = obligations.merge_summaries(
        [
            {
                "blocking_total": 1,
                "advisory_total": 0,
                "unsettled_total": 1,
                "by_type": {"constitution_stale": 1},
                "by_depth": {"1": 1},
                "oldest_blocking_age_seconds": 30.0,
                "revalidate_queue_depth": 0,
                "settled_by_human_count": 0,
                "settled_total": 0,
                "recurrence_total": 0,
            },
            {
                "blocking_total": 2,
                "advisory_total": 1,
                "unsettled_total": 3,
                "by_type": {"integration_conflict": 2, "constitution_stale": 1},
                "by_depth": {"1": 1, "2": 2},
                "oldest_blocking_age_seconds": 400.0,
                "revalidate_queue_depth": 2,
                "settled_by_human_count": 1,
                "settled_total": 3,
                "recurrence_total": 4,
            },
        ]
    )
    assert merged["blocking_total"] == 3
    assert merged["advisory_total"] == 1
    assert merged["by_type"] == {"constitution_stale": 2, "integration_conflict": 2}
    assert merged["by_depth"] == {"1": 2, "2": 2}
    assert merged["oldest_blocking_age_seconds"] == 400.0
    assert merged["revalidate_queue_depth"] == 2
    assert merged["settled_by_human_count"] == 1


def test_tree_level_merge_of_nothing_keeps_the_keys_and_stays_honest() -> None:
    merged = obligations.merge_summaries([])
    assert merged["blocking_total"] == 0
    assert merged["by_type"] == {}
    assert merged["oldest_blocking_age_seconds"] is None

