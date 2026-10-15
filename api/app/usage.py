"""供应商无关的用量与成本记账。

存在的理由：Codex CLI 只在 `--json` 的 `turn.completed` 里报告 **token**；而自建网关
常在**自己的响应**里额外报告**金额**，且字段名、币种、计价口径都可能随供应商变化。
若把某一家供应商的形状写进业务代码，换供应商时就要改多处。

本模块是唯一的换算边界：

- 入参是**任意供应商**给出的 usage/cost 原始字典（允许 None、允许缺字段、允许脏数据）；
- 出参是统一的规范形状，并**把原始负载完整保留在 `raw`**，换供应商时不丢信息；
- 金额按**币种分桶**（`cost_by_currency`），避免把 CNY 与 USD 直接相加。

新增供应商时只需要在 `_FIELD_CANDIDATES` / `_cost_of` 里补候选键，不需要改调用方。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# 规范字段 -> 各供应商可能使用的候选键（按优先级）。
_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "prompt_tokens", "input"),
    "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens", "cached_tokens"),
    "cache_write_tokens": ("cache_write_input_tokens", "cache_creation_input_tokens", "cache_write_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens", "output"),
    "reasoning_tokens": ("reasoning_output_tokens", "reasoning_tokens"),
    "total_tokens": ("total_tokens", "total"),
}

CURRENCY_FIELD_CANDIDATES = ("currency", "cost_currency", "currency_code")

# 已知的嵌套容器：token 常放在这些键下，金额常放在这些键下的 `cost` 里。
_TOKEN_CONTAINER_KEYS = ("usage", "token_usage")
# 只用**通用**容器名。某家网关的历史字段名不该进业务代码：
# 换供应商时那种键会变成「读不到金额」的静默缺口。
_COST_CONTAINER_KEYS = ("gateway", "billing", "provider", "metadata", "cost")

CANONICAL_TOKEN_FIELDS = tuple(_FIELD_CANDIDATES) + ("total_tokens",)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _dig(source: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = source
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _pick_tokens(payload: Mapping[str, Any], field: str) -> int | None:
    for key in _FIELD_CANDIDATES[field]:
        found = _as_int(payload.get(key))
        if found is not None:
            return found
    # 嵌套形态：{"prompt_tokens_details": {"cached_tokens": N}}
    if field == "cached_input_tokens":
        nested = _dig(payload, ("prompt_tokens_details", "cached_tokens"))
        found = _as_int(nested)
        if found is not None:
            return found
    if field == "reasoning_tokens":
        nested = _dig(payload, ("output_tokens_details", "reasoning_tokens"))
        found = _as_int(nested)
        if found is not None:
            return found
    return None


def _token_view(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """把嵌套的 token 容器摊平到顶层，顶层字段优先。"""
    for key in _TOKEN_CONTAINER_KEYS:
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            merged.update({name: value for name, value in raw.items() if name not in _TOKEN_CONTAINER_KEYS})
            return merged
    return raw


def _cost_of(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """从任意形状里提取金额。找不到就返回 None（只有 token 也很正常）。"""
    found = _cost_of_flat(payload)
    if found is not None:
        return found
    for key in _COST_CONTAINER_KEYS:
        container = payload.get(key)
        if isinstance(container, Mapping):
            found = _cost_of_flat(container)
            if found is not None:
                found.setdefault("container", key)
                return found
    return None


def _cost_of_flat(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    currency = None
    for key in CURRENCY_FIELD_CANDIDATES:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            currency = value.strip().upper()
            break

    # 形态 A：{"cost": 0.0123}
    direct = _as_float(payload.get("cost"))
    if direct is not None:
        return {"amount": direct, "currency": currency, "source": "flat"}

    cost_node = payload.get("cost")
    if isinstance(cost_node, Mapping):
        # 形态 B：{"cost": {"total_credit": 0.010157, "currency": "CNY"}}
        for key in ("total_credit", "total", "amount", "total_cost", "credit"):
            amount = _as_float(cost_node.get(key))
            if amount is None:
                continue
            nested_currency = cost_node.get("currency") or currency
            if isinstance(nested_currency, str):
                nested_currency = nested_currency.strip().upper() or None
            return {"amount": amount, "currency": nested_currency, "source": "cost_object"}
        # 形态 C：{"cost": {"details": [{"credit": ..., "currency": "CNY"}]}}
        details = cost_node.get("details")
        if isinstance(details, list):
            totals: dict[str, float] = {}
            for item in details:
                if not isinstance(item, Mapping):
                    continue
                amount = _as_float(item.get("credit"))
                if amount is None:
                    continue
                code = item.get("currency")
                code = code.strip().upper() if isinstance(code, str) and code.strip() else nested_currency
                totals[code or "UNKNOWN"] = totals.get(code or "UNKNOWN", 0.0) + amount
            if totals:
                return {"amount": sum(totals.values()), "currency": currency, "by_currency": totals, "source": "cost_details"}

    # 形态 D：供应商把金额放在别的键名上
    for key in ("total_cost", "cost_usd", "cost_cny"):
        amount = _as_float(payload.get(key))
        if amount is None:
            continue
        guessed = "USD" if key == "cost_usd" else "CNY" if key == "cost_cny" else currency
        return {"amount": amount, "currency": guessed, "source": key}

    return None


def normalize_usage(raw: Any) -> dict[str, Any] | None:
    """把任意供应商的 usage 规范化为统一形状；无法识别时返回 None。

    原始负载保存在 `raw`，因此换供应商或上游字段变化时不会丢信息。
    """
    if not isinstance(raw, Mapping) or not raw:
        return None
    view = _token_view(raw)
    tokens = {field: _pick_tokens(view, field) for field in _FIELD_CANDIDATES}
    if all(value is None for value in tokens.values()) and _cost_of(raw) is None:
        return None
    if tokens["total_tokens"] is None:
        parts = [tokens.get("input_tokens"), tokens.get("output_tokens")]
        if any(part is not None for part in parts):
            tokens["total_tokens"] = sum(part or 0 for part in parts)
    normalized: dict[str, Any] = {field: value for field, value in tokens.items() if value is not None}
    cost = _cost_of(raw)
    if cost is not None:
        normalized["cost"] = cost
    normalized["raw"] = dict(raw)
    return normalized


def _token_totals(items: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    totals = {field: 0 for field in _FIELD_CANDIDATES}
    for item in items:
        for field in totals:
            value = item.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[field] += value
    return totals


def summarize_usages(items: Iterable[Any]) -> dict[str, Any]:
    """汇总多份 usage。金额**按币种分桶**，不做跨币种相加。"""
    normalized = [entry for entry in (normalize_usage(item) for item in items) if entry is not None]
    if not normalized:
        return {}
    totals = _token_totals(normalized)
    summary: dict[str, Any] = {"runs": len(normalized)}
    summary.update({field: value for field, value in totals.items() if value})
    by_currency: dict[str, float] = {}
    unlabeled = 0.0
    for entry in normalized:
        cost = entry.get("cost")
        if not isinstance(cost, Mapping):
            continue
        amount = cost.get("amount")
        if not isinstance(amount, (int, float)):
            continue
        bucket = cost.get("currency")
        if isinstance(bucket, str) and bucket:
            by_currency[bucket] = by_currency.get(bucket, 0.0) + float(amount)
        else:
            unlabeled += float(amount)
        for code, value in (cost.get("by_currency") or {}).items():
            if isinstance(value, (int, float)):
                by_currency[code] = by_currency.get(code, 0.0) + float(value)
    if by_currency or unlabeled:
        summary["cost_by_currency"] = {code: round(value, 6) for code, value in sorted(by_currency.items())}
        if unlabeled:
            summary["cost_unlabeled"] = round(unlabeled, 6)
    return summary
