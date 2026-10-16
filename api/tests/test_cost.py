"""花费归因（P3-3）与成本预测（P3-2）的判据。

这一层最容易犯的错**不是算错**，而是**编出看起来合理的数**：

* 取不到模型名 → 伪造 `model="unknown"` 桶，于是「47% 属于 unknown」看起来像结论；
* 供应商没报金额 → 归因表全是 0，看起来像「没花过钱」；
* 样本只有 1 个节点 → 给出一个精确到小数点的"预计还需 12.3456 元"；
* 两个币种 → 加成一个总额，那个数字在任何币种下都不成立。

所以下面的判据大多是「**不许报什么**」。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| C1 | 模型名取不到 → 计数单列，**不造假桶** | 假桶让"没记录"看起来像统计结论 |
| C2 | 有运行但无用量 → 单独计数 | 归因表看起来像"这棵树没花过钱" |
| C3 | 归因按节点/角色/模型三维拆，且最贵的排前面 | 回答不了"钱花在哪一层" |
| C4 | 币种分开，不跨币种相加 | 混币种总额在任何币种下都是错的 |
| C5 | 预测给**区间**并报样本量 | 单一数字被读成"确定要花这么多" |
| C6 | 样本不足 → **拒绝给数** + 原因 | 凭空的数字让人放心去睡觉 |
| C7 | 预测只按单一币种，不做汇率假设 | 悄悄引入一个汇率，没人知道用了哪个 |
| C8 | 重试率上限有界 | 无界放大会让预测失去参考价值 |
"""
from __future__ import annotations

import pytest

from app import cost
from app.usage import normalize_usage


# ---------- C1 模型名 ----------


def test_c1_model_is_read_from_flat_and_nested_payloads() -> None:
    """模型名三种常见放法都要认出来：顶层、网关容器里、`model_info` 里。"""
    assert normalize_usage({"model": "deepseek-v4.1", "input_tokens": 1})["model"] == "deepseek-v4.1"
    assert (
        normalize_usage({"gateway": {"model_name": "x-model"}, "input_tokens": 1})["model"]
        == "x-model"
    )
    assert (
        normalize_usage({"model_info": {"model_id": "y-model"}, "input_tokens": 1})["model"]
        == "y-model"
    )


def test_c1_missing_model_is_absent_not_a_placeholder() -> None:
    """取不到模型名时**整个键不出现**，不填 "unknown"。"""
    entry = normalize_usage({"input_tokens": 5, "output_tokens": 1})
    assert "model" not in entry


def test_c1_attribution_counts_runs_without_a_model_separately() -> None:
    """没有模型名的运行进 `runs_unknown_model`，**不**进 `by_model`。

    这是本文件最要紧的一条：造假桶会让「47% 的花费属于 unknown」看起来像一个
    统计结论，而真相是「这些运行没记录模型」—— 前者让人去优化，后者让人去修记录。
    """
    attribution = cost.Attribution()
    attribution.add(node_id="a", kind="worker", usage={"model": "m1", "input_tokens": 10})
    attribution.add(node_id="b", kind="worker", usage={"input_tokens": 10})

    dumped = attribution.dump()
    assert dumped["runs_unknown_model"] == 1
    assert [bucket["key"] for bucket in dumped["by_model"]] == ["m1"]
    assert all("unknown" not in bucket["key"] for bucket in dumped["by_model"])


# ---------- C2 无用量 ----------


def test_c2_runs_without_usage_are_counted_not_silently_dropped() -> None:
    """一条用量都识别不出来时要单独报数 —— 否则归因表像「没花过钱」。"""
    attribution = cost.Attribution()
    attribution.add(node_id="a", kind="worker", usage=None)
    attribution.add(node_id="a", kind="worker", usage={})
    attribution.add(node_id="a", kind="worker", usage={"与用量无关": 1})

    dumped = attribution.dump()
    assert dumped["runs"] == 3
    assert dumped["runs_with_usage"] == 0
    assert dumped["runs_without_usage"] == 3
    assert dumped["total_tokens"] == 0
    # 没有金额时是 None，不是 {} —— "没记账"与"记了 0 元"不同。
    assert dumped["cost_by_currency"] is None


# ---------- C3 三维归因 ----------


def test_c3_attribution_splits_by_node_kind_and_model() -> None:
    attribution = cost.Attribution()
    attribution.add(
        node_id="root",
        kind="planner",
        usage={"model": "strong", "input_tokens": 100, "cost": {"amount": 1.0, "currency": "USD"}},
        node_title="根目标",
        node_depth=0,
    )
    attribution.add(
        node_id="leaf-1",
        kind="worker",
        usage={"model": "cheap", "input_tokens": 900, "cost": {"amount": 0.1, "currency": "USD"}},
        node_title="子任务一",
        node_depth=1,
    )
    attribution.add(
        node_id="leaf-1",
        kind="verifier",
        usage={"model": "strong", "input_tokens": 50, "cost": {"amount": 0.2, "currency": "USD"}},
    )

    dumped = attribution.dump()
    assert dumped["total_tokens"] == 1050
    assert dumped["cost_by_currency"] == {"USD": 1.3}

    by_kind = {bucket["key"]: bucket for bucket in dumped["by_kind"]}
    assert by_kind["worker"]["runs"] == 1
    assert by_kind["verifier"]["total_tokens"] == 50
    assert by_kind["planner"]["cost_by_currency"] == {"USD": 1.0}

    by_model = {bucket["key"]: bucket for bucket in dumped["by_model"]}
    assert by_model["cheap"]["total_tokens"] == 900
    assert by_model["strong"]["runs"] == 2


def test_c3_most_expensive_node_comes_first_and_keeps_readable_fields() -> None:
    """归因表第一行就该回答「钱主要花在哪」，且要带得出节点标题。"""
    attribution = cost.Attribution()
    attribution.add(node_id="cheap-node", kind="worker", usage={"input_tokens": 1})
    attribution.add(
        node_id="pricey-node", kind="worker", usage={"input_tokens": 999}, node_title="最贵的那个", node_depth=2
    )

    by_node = attribution.dump()["by_node"]
    assert [item["node_id"] for item in by_node] == ["pricey-node", "cheap-node"]
    assert by_node[0]["title"] == "最贵的那个"
    assert by_node[0]["depth"] == 2
    # 模型/角色桶不该带节点专属字段（带了会让界面误以为它们也有标题）。
    assert "title" not in attribution.dump()["by_kind"][0]


def test_c3_node_title_survives_a_later_run_without_a_title() -> None:
    """后一条运行没给标题时，不能把已有的标题抹成空。"""
    attribution = cost.Attribution()
    attribution.add(node_id="n", kind="worker", usage={"input_tokens": 1}, node_title="有标题")
    attribution.add(node_id="n", kind="worker", usage={"input_tokens": 1})
    assert attribution.dump()["by_node"][0]["title"] == "有标题"


# ---------- C4 币种 ----------


def test_c4_currencies_are_never_summed_across() -> None:
    """CNY 与 USD 各留一个桶；没有币种标签的金额进 UNKNOWN 桶（如实标明）。"""
    attribution = cost.Attribution()
    attribution.add(node_id="a", kind="worker", usage={"cost": {"amount": 1.0, "currency": "cny"}})
    attribution.add(node_id="a", kind="worker", usage={"cost": {"amount": 2.0, "currency": "USD"}})
    attribution.add(node_id="a", kind="worker", usage={"cost": {"amount": 0.5}})

    assert attribution.dump()["cost_by_currency"] == {"CNY": 1.0, "USD": 2.0, "UNKNOWN": 0.5}


def test_c4_shares_are_within_one_currency_and_empty_when_uncomputable() -> None:
    assert cost.shares({"USD": 75.0, "EUR": 25.0}) == {"EUR": 25.0, "USD": 75.0}
    assert cost.shares(None) == {}
    assert cost.shares({"USD": 0}) == {}


# ---------- C5 / C6 预测 ----------


def test_c5_projection_reports_a_range_and_the_sample_size() -> None:
    result = cost.project_cost(
        settled_costs=[1.0, 3.0],
        settled_nodes=2,
        remaining_nodes=10,
        currency="USD",
    )
    assert result["available"] is True
    assert result["unit_cost"] == 2.0
    assert result["settled_cost"] == 4.0
    assert result["low"] == 20.0
    assert result["mid"] == 20.0
    assert result["high"] == 40.0
    # 样本量必须一起报：2 个节点算出的"平均单价"和 200 个的说服力不同。
    assert result["sample_nodes"] == 2
    assert result["currency"] == "USD"
    assert "不含" in result["note"]


def test_c5_retry_factor_widens_the_range() -> None:
    result = cost.project_cost(
        settled_costs=[1.0], settled_nodes=1, remaining_nodes=4, retry_factor=1.5, currency="USD"
    )
    assert result["low"] == 4.0
    assert result["mid"] == 6.0
    assert result["high"] == 12.0
    assert result["retry_factor"] == 1.5


def test_c6_no_settled_nodes_refuses_to_project() -> None:
    """没有样本就**拒绝**，不给 0 —— 0 会被读成「不用再花钱」。"""
    result = cost.project_cost(settled_costs=[], settled_nodes=0, remaining_nodes=5, currency="USD")
    assert result["available"] is False
    assert result["reason"]
    assert "low" not in result


def test_c6_settled_nodes_without_any_money_refuses_to_project_money() -> None:
    """有节点但供应商没报金额 → 只能报 token，不能报钱。"""
    result = cost.project_cost(
        settled_costs=[], settled_nodes=3, remaining_nodes=5, currency="USD"
    )
    assert result["available"] is False
    assert "计价" in result["reason"]


def test_c6_zero_remaining_nodes_is_available_and_zero() -> None:
    """没有剩余节点是**真的 0**，与「算不出来」不同 —— 这里必须给 0。"""
    result = cost.project_cost(
        settled_costs=[2.0], settled_nodes=1, remaining_nodes=0, currency="USD"
    )
    assert result["available"] is True
    assert result["low"] == result["mid"] == result["high"] == 0.0
    assert result["settled_nodes"] == 1


def test_c7_projection_is_single_currency_only() -> None:
    """预测里的币种是**输入**、原样带出；本模块不做任何汇率假设。"""
    result = cost.project_cost(
        settled_costs=[1.0], settled_nodes=1, remaining_nodes=2, currency="CNY"
    )
    assert result["currency"] == "CNY"
    # 换一个币种只是换个标签，数值不会因此变化（没有隐式换算）。
    other = cost.project_cost(
        settled_costs=[1.0], settled_nodes=1, remaining_nodes=2, currency="USD"
    )
    assert other["low"] == result["low"]


# ---------- C8 重试率 ----------


@pytest.mark.parametrize(
    ("retry_count", "node_count", "expected"),
    [
        (0, 0, 1.0),
        (0, 10, 1.0),
        (5, 10, 1.5),
        (100, 10, 3.0),  # 上限有界
        (-3, 10, 1.0),  # 负数不该放大，也不该缩到 0 以下
    ],
)
def test_c8_retry_factor_is_bounded(retry_count: int, node_count: int, expected: float) -> None:
    assert cost.retry_factor_for(retry_count=retry_count, node_count=node_count) == expected


def test_c8_unknown_retry_data_does_not_kill_the_projection() -> None:
    """重试率未知时乘数是 1.0，预测仍然可用 —— 但要如实显示成 1.0。"""
    factor = cost.retry_factor_for(retry_count=0, node_count=0)
    assert factor == 1.0
    result = cost.project_cost(
        settled_costs=[1.0], settled_nodes=1, remaining_nodes=3, retry_factor=factor
    )
    assert result["available"] is True
    assert result["retry_factor"] == 1.0


# ---------- 状态集合本身 ----------


def test_terminal_and_settled_sets_are_separate_and_sane() -> None:
    """`blocked` 是终态但**不是**已结算：它花过钱却没有产出，不该进单价样本。

    这条挡的是"把被阻塞的节点也当成一个完成单位"——那会把平均单价算低，
    于是预测偏乐观，而乐观的预测正是最坏的方向。
    """
    assert "blocked" in cost.TERMINAL_STATUSES
    assert "blocked" not in cost.SETTLED_STATUSES
    assert cost.SETTLED_STATUSES < cost.TERMINAL_STATUSES
