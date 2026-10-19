"""Fail-closed input checks shared by scene declarations and worker submissions."""
from __future__ import annotations

import math

INTEGER_PARAMS = frozenset({"tiers", "planks", "steps", "mullions", "rails"})
POSITIVE_PARAMS = frozenset({"w", "h", "t", "length", "depth", "size", "height", "tier_h", "base_h", "merlon_w", "merlon_h", "gap", "sink", "bar", "protrude", "band_h"})


def finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def require_finite_tree(value: object, path: str = "input") -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError(f"{path}: non-finite number")
    elif isinstance(value, dict):
        for key, child in value.items():
            require_finite_tree(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, child in enumerate(value):
            require_finite_tree(child, f"{path}[{i}]")


def require_geometry_params(params: dict) -> None:
    require_finite_tree(params)
    for key, value in params.items():
        if key == "openings":
            if not isinstance(value, list):
                raise ValueError("openings must be a list")
            for opening in value:
                if not isinstance(opening, dict):
                    raise ValueError("opening must be an object")
                for dim in ("w", "h"):
                    if not finite_number(opening.get(dim)) or opening[dim] <= 0:
                        raise ValueError(f"opening.{dim} must be positive")
        elif key in POSITIVE_PARAMS:
            if not finite_number(value) or value <= 0:
                raise ValueError(f"{key} must be a positive finite number")
        elif key in INTEGER_PARAMS:
            if not finite_number(value) or value != int(value):
                raise ValueError(f"{key} must be a finite integer")
    if "x0" in params and "x1" in params and params["x1"] <= params["x0"]:
        raise ValueError("x1 must be greater than x0")
