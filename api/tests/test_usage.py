from __future__ import annotations

from app.usage import normalize_usage, summarize_usages


def test_normalize_codex_shaped_usage() -> None:
    raw = {
        "input_tokens": 14108,
        "cached_input_tokens": 13440,
        "cache_write_input_tokens": 0,
        "output_tokens": 801,
        "reasoning_output_tokens": 293,
    }
    normalized = normalize_usage(raw)
    assert normalized is not None
    assert normalized["input_tokens"] == 14108
    assert normalized["cached_input_tokens"] == 13440
    assert normalized["output_tokens"] == 801
    assert normalized["reasoning_tokens"] == 293
    assert normalized["total_tokens"] == 14909
    assert normalized["raw"] == raw
    assert "cost" not in normalized


def test_normalize_openai_shaped_usage() -> None:
    normalized = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 64},
            "output_tokens_details": {"reasoning_tokens": 7},
        }
    )
    assert normalized is not None
    assert normalized["input_tokens"] == 100
    assert normalized["output_tokens"] == 20
    assert normalized["cached_input_tokens"] == 64
    assert normalized["reasoning_tokens"] == 7
    assert normalized["total_tokens"] == 120


def test_normalize_rejects_unrecognizable_payloads() -> None:
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None
    assert normalize_usage("nope") is None
    assert normalize_usage({"unrelated": "field"}) is None


def test_normalize_extracts_flat_cost_with_currency() -> None:
    normalized = normalize_usage({"input_tokens": 1, "cost": "0.0123", "currency": "cny"})
    assert normalized is not None
    assert normalized["cost"] == {"amount": 0.0123, "currency": "CNY", "source": "flat"}


def test_normalize_extracts_nested_token_container_and_gateway_cost() -> None:
    normalized = normalize_usage(
        {
            "usage": {"input_tokens": 97, "output_tokens": 25},
            "aigw": {"cost": {"total_credit": "0.010157", "currency": "CNY"}},
        }
    )
    assert normalized is not None
    assert normalized["input_tokens"] == 97
    assert normalized["output_tokens"] == 25
    assert normalized["cost"]["amount"] == 0.010157
    assert normalized["cost"]["currency"] == "CNY"
    assert normalized["cost"]["container"] == "aigw"


def test_normalize_extracts_cost_details_per_currency() -> None:
    normalized = normalize_usage(
        {
            "input_tokens": 97,
            "cost": {
                "details": [
                    {"credit": 0.005432, "currency": "CNY"},
                    {"credit": 0.001, "currency": "USD"},
                    {"credit": 0.002, "currency": "CNY"},
                ]
            },
        }
    )
    assert normalized is not None
    cost = normalized["cost"]
    assert cost["source"] == "cost_details"
    assert cost["by_currency"] == {"CNY": 0.007432, "USD": 0.001}


def test_summarize_totals_tokens_and_buckets_cost_by_currency() -> None:
    summary = summarize_usages(
        [
            {"input_tokens": 10, "output_tokens": 1, "cost": 0.01, "currency": "CNY"},
            {"prompt_tokens": 20, "completion_tokens": 2, "cost": 0.02, "currency": "USD"},
            {"input_tokens": 5, "output_tokens": 1, "cost": 0.03, "currency": "CNY"},
            None,
            "garbage",
        ]
    )
    assert summary["runs"] == 3
    assert summary["input_tokens"] == 35
    assert summary["output_tokens"] == 4
    # 不同币种必须分桶，不能相加成 0.06
    assert summary["cost_by_currency"] == {"CNY": 0.04, "USD": 0.02}


def test_summarize_returns_empty_for_no_usable_entries() -> None:
    assert summarize_usages([]) == {}
    assert summarize_usages([None, {}, "x"]) == {}
