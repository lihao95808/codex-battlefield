"""构件库（builder catalog）：只把参数翻成实心盒，不做任何造型判断。

约定与战场其它模块一致：**参数错误在这里就抛，不静默放过**。
洞（窗/门）不是 CSG 减法，而是在 `wall_panel` 里被分解成若干盒带：
这样"实体 = 一组实心盒"的不变式对下游（点内测试、GLB、渲染）全部成立。
"""

from __future__ import annotations

from .boxes import Box, box

# 材质码：1 石墙 / 2 石线脚 / 3 木 / 4 金属窗棂 / 5 塔顶瓦
MATERIAL = {
    "wall_panel": 1,
    "battlement": 1,
    "tower": 1,
    "moulding": 2,
    "door_leaf": 3,
    "lattice": 4,
}

# 每个 builder 的必填键与允许域。domain 只约束"自由参数"，受约束参数由声明逐字核对。
DOMAINS: dict[str, dict[str, tuple[float, float]]] = {
    "battlement": {"merlon_w": (0.4, 1.2), "gap": (0.3, 1.2), "merlon_h": (0.4, 1.4), "base_h": (0.1, 0.5), "y0": (0.0, 0.4)},
    "tower": {"tiers": (2, 6), "tier_h": (0.3, 0.7)},
    "door_leaf": {"planks": (3, 10), "sink": (0.01, 0.12), "t": (0.08, 0.25)},
    "lattice": {"sink": (0.01, 0.05), "bar": (0.03, 0.10), "mullions": (0, 4), "rails": (0, 3)},
    "moulding": {"steps": (1, 2)},
}

REQUIRED = {
    "wall_panel": ("w", "h", "t", "openings"),
    "battlement": ("length", "depth", "base_h", "merlon_w", "gap", "merlon_h", "y0"),
    "tower": ("size", "height", "tiers", "tier_h"),
    "door_leaf": ("w", "h", "t", "planks", "sink"),
    "moulding": ("x0", "x1", "z", "t", "protrude", "band_h", "steps"),
    "lattice": ("cx", "cz", "w", "h", "t", "sink", "bar", "mullions", "rails"),
}


def _f(params: dict, key: str) -> float:
    try:
        return float(params[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"param {key!r} missing or not numeric: {params.get(key)!r}") from exc


def _i(params: dict, key: str) -> int:
    v = _f(params, key)
    if abs(v - round(v)) > 1e-9:
        raise ValueError(f"param {key!r} must be an integer, got {v}")
    return int(round(v))


def wall_panel(p: dict) -> tuple[Box, ...]:
    """墙板挖矩形洞。按 x 切带、每条带里按 z 求补集——精确分解，不留 CSG。"""
    w, h, t = _f(p, "w"), _f(p, "h"), _f(p, "t")
    holes: list[tuple[float, float, float, float]] = []
    for o in p.get("openings") or []:
        x0, z0 = _f(o, "x"), _f(o, "z")
        holes.append((x0, z0, x0 + _f(o, "w"), z0 + _f(o, "h")))

    xs = sorted({0.0, w} | {c for hxz in holes for c in (hxz[0], hxz[2]) if 0.0 < c < w})
    out: list[Box] = []
    for a, b in zip(xs, xs[1:], strict=False):
        if b - a <= 1e-9:
            continue
        strip = [hz for hz in holes if hz[0] <= a + 1e-9 and hz[2] >= b - 1e-9]
        zs = sorted({0.0, h} | {c for hz in strip for c in (hz[1], hz[3]) if 0.0 < c < h})
        for c, d in zip(zs, zs[1:], strict=False):
            if d - c <= 1e-9:
                continue
            mid = (c + d) / 2
            if any(hz[1] <= mid <= hz[3] for hz in strip):
                continue
            out.append(box(a, 0.0, c, b, t, d))
    if not out:
        raise ValueError("wall_panel produced no solid: openings cover the whole panel")
    return tuple(out)


def battlement(p: dict) -> tuple[Box, ...]:
    length, depth = _f(p, "length"), _f(p, "depth")
    base_h, merlon_w = _f(p, "base_h"), _f(p, "merlon_w")
    gap, merlon_h = _f(p, "gap"), _f(p, "merlon_h")
    y0 = _f(p, "y0")
    out = [box(0.0, y0, 0.0, length, y0 + depth, base_h)]
    period = merlon_w + gap
    n = max(1, int((length + gap) // period))
    span = n * merlon_w + (n - 1) * gap
    start = max(0.0, (length - span) / 2.0)
    for i in range(n):
        x = start + i * period
        out.append(box(x, y0, base_h, x + merlon_w, y0 + depth, base_h + merlon_h))
    return tuple(out)


def tower(p: dict) -> tuple[Box, ...]:
    size, height = _f(p, "size"), _f(p, "height")
    tiers, tier_h = _i(p, "tiers"), _f(p, "tier_h")
    out = [box(0.0, 0.0, 0.0, size, size, height)]
    z, cur = height, size
    for _ in range(tiers):
        inset = min(0.32, cur * 0.16)
        if cur - 2 * inset <= 1e-6:
            break
        out.append(box(inset, inset, z, cur - inset, cur - inset, z + tier_h))
        z += tier_h
        cur -= 2 * inset
    if cur > 0.4:
        out.append(box(cur * 0.3, cur * 0.3, z, cur * 0.7, cur * 0.7, z + 0.6))
    return tuple(out)


def door_leaf(p: dict) -> tuple[Box, ...]:
    w, h, t = _f(p, "w"), _f(p, "h"), _f(p, "t")
    planks, sink = _i(p, "planks"), _f(p, "sink")
    out: list[Box] = []
    pw = (w + 2 * sink) / planks
    for i in range(planks):
        x = -sink + i * pw
        out.append(box(x + 0.008, 0.0, 0.0, x + pw - 0.008, t, h + sink))
    for zb in (0.45, h - 0.55):
        out.append(box(-sink, -0.02, zb, w + sink, t + 0.02, zb + 0.12))
    return tuple(out)


def moulding(p: dict) -> tuple[Box, ...]:
    x0, x1, z = _f(p, "x0"), _f(p, "x1"), _f(p, "z")
    t = _f(p, "t")
    protrude, band_h = _f(p, "protrude"), _f(p, "band_h")
    steps = _i(p, "steps")
    embed = 0.02
    if steps == 1:
        return (box(x0, t - embed, z, x1, t + protrude, z + band_h),)
    half = band_h / 2.0
    inner = max(protrude * 0.55, 0.015)
    return (
        box(x0, t - embed, z, x1, t + protrude, z + half),
        box(x0, t - embed, z + half, x1, t + inner, z + band_h),
    )


def lattice(p: dict) -> tuple[Box, ...]:
    cx, cz = _f(p, "cx"), _f(p, "cz")
    w, h, t = _f(p, "w"), _f(p, "h"), _f(p, "t")
    sink, bar = _f(p, "sink"), _f(p, "bar")
    mullions, rails = _i(p, "mullions"), _i(p, "rails")
    d = min(0.06, t * 0.5)
    y0, y1 = t / 2.0 - d / 2.0, t / 2.0 + d / 2.0
    ax0, az0 = cx - w / 2 - sink, cz - h / 2 - sink
    ax1, az1 = cx + w / 2 + sink, cz + h / 2 + sink
    ox0, oz0 = cx - w / 2 + sink, cz - h / 2 + sink
    ox1, oz1 = cx + w / 2 - sink, cz + h / 2 - sink
    out: list[Box] = [
        box(ax0, y0, az0, ax1, y1, az0 + bar),
        box(ax0, y0, az1 - bar, ax1, y1, az1),
        box(ax0, y0, az0, ax0 + bar, y1, az1),
        box(ax1 - bar, y0, az0, ax1, y1, az1),
    ]
    for i in range(mullions):
        x = ox0 + (ox1 - ox0) * (i + 1) / (mullions + 1)
        out.append(box(x - bar / 2, y0, az0 + bar, x + bar / 2, y1, az1 - bar))
    for j in range(rails):
        z = oz0 + (oz1 - oz0) * (j + 1) / (rails + 1)
        out.append(box(ox0 + bar, y0, z - bar / 2, ox1 - bar, y1, z + bar / 2))
    return tuple(out)


BUILDERS = {
    "wall_panel": wall_panel,
    "battlement": battlement,
    "tower": tower,
    "door_leaf": door_leaf,
    "moulding": moulding,
    "lattice": lattice,
}


def build(part: dict) -> tuple[Box, ...]:
    """把一条构件规格（{slot, builder, params}）变成实心盒组。参数错直接抛 ValueError。"""
    name = part.get("builder")
    if name not in BUILDERS:
        raise ValueError(f"unknown builder {name!r}")
    params = part.get("params")
    if not isinstance(params, dict):
        raise ValueError(f"params of {part.get('slot')!r} must be an object")
    missing = [k for k in REQUIRED[name] if k not in params]
    if missing:
        raise ValueError(f"{name} missing params {missing}")
    return BUILDERS[name](params)
