"""两道判定：① 填写是否合规（结构化拒绝）② 几何是否真的做出来了（特征校验）。

失效方向（AGENTS.md 硬约定 5）——**两条都 fail closed**：
- 拿不到 parts / 槽位缺失 / 参数越域 / builder 不存在 → 阻断，不"按默认值继续"。
- 特征区域取不到样本（例如声明里写了个零体积盒）→ **判失败**，绝不因为"没采样到"就通过。
  这条是防"恒真断言"的关键：空区域会让比例恒为 0，而 0 恰好等于"空洞"的期望值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .assembly import AssemblyError, PlacedPart, connectivity, place, world_region
from .contracts import finite_number, require_geometry_params
from .boxes import Box, box
from .builders import DOMAINS, REQUIRED
from .declaration import DEFAULT_POS_TOL, DEFAULT_TOL, Declaration
from .solid import SolidIndex, region_fraction

POS_KEYS = ("x0", "x1", "z", "cx", "cz")


@dataclass(frozen=True)
class Reject:
    slot: str
    field: str
    expected: Any
    actual: Any
    rule: str
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {"slot": self.slot, "field": self.field, "expected": self.expected, "actual": self.actual, "rule": self.rule}
        if self.why:
            d["why"] = self.why
        return d


def _num(v: Any) -> float | None:
    if not finite_number(v):
        return None
    return float(v)


def _tol_for(key: str) -> float:
    return DEFAULT_POS_TOL if key in POS_KEYS else DEFAULT_TOL


def _match_openings(expect: list[dict], actual: list[Any]) -> list[tuple[int, dict, dict | None]]:
    """把声明洞口与提交洞口按最近中心配对（顺序不重要）。返回 (索引, 期望, 匹配到的洞口或 None)。"""
    used: set[int] = set()
    pairs = []
    for i, e in enumerate(expect):
        best, best_d = None, 1e9
        for j, a in enumerate(actual):
            if j in used or not isinstance(a, dict):
                continue
            ax, az = _num(a.get("x")), _num(a.get("z"))
            if ax is None or az is None:
                continue
            d = abs(ax - float(e["x"])) + abs(az - float(e["z"]))
            if d < best_d:
                best, best_d = j, d
        if best is None or best_d > 0.12:
            pairs.append((i, e, None))
        else:
            used.add(best)
            pairs.append((i, e, actual[best] if isinstance(actual[best], dict) else None))
    return pairs


def validate_parts(decl: Declaration, parts: Any) -> list[Reject]:
    """把 worker 提交的构件表对着声明逐项核对，返回结构化拒绝（空列表 = 合规）。"""
    if not isinstance(parts, list) or not parts:
        return [Reject("*", "parts", "非空数组", type(parts).__name__, "shape")]

    seen: dict[str, dict] = {}
    rejects: list[Reject] = []
    for idx, part in enumerate(parts):
        if not isinstance(part, dict):
            rejects.append(Reject("*", f"parts[{idx}]", "对象", type(part).__name__, "shape"))
            continue
        slot = str(part.get("slot") or "")
        if not slot:
            rejects.append(Reject("*", f"parts[{idx}].slot", "槽位名", part.get("slot"), "shape"))
            continue
        if slot in seen:
            rejects.append(Reject(slot, "slot", "唯一", "重复出现", "duplicate-slot"))
        seen[slot] = part
        declared = decl.by_name(slot)
        if declared is None:
            rejects.append(Reject(slot, "slot", "声明里没有这个槽位", slot, "undeclared-slot"))
            continue
        if part.get("builder") != declared.builder:
            rejects.append(Reject(slot, "builder", declared.builder, part.get("builder"), "builder-mismatch"))

    for slot in decl.slots:
        if slot.name not in seen:
            rejects.append(Reject(slot.name, "slot", "必须存在", "缺失", "missing-slot"))

    for slot in decl.slots:
        part = seen.get(slot.name)
        if part is None or part.get("builder") != slot.builder:
            continue
        params = part.get("params")
        if not isinstance(params, dict):
            rejects.append(Reject(slot.name, "params", "对象", type(params).__name__, "shape"))
            continue
        try:
            require_geometry_params(params)
        except (ValueError, TypeError, OverflowError) as exc:
            rejects.append(Reject(slot.name, "params", "finite valid geometry", str(exc), "geometry-domain"))
        for key in REQUIRED[slot.builder]:
            if key not in params:
                expected = slot.expect.get(key, f"free:{slot.domain.get(key)}")
                rejects.append(Reject(slot.name, f"params.{key}", expected, "缺失", "missing-param"))
        for key, want in slot.expect.items():
            if key == "openings":
                continue
            got = _num(params.get(key))
            if got is None:
                if key in params:
                    rejects.append(Reject(slot.name, f"params.{key}", want, params.get(key), "not-a-number"))
                continue
            if abs(got - float(want)) > _tol_for(key):
                rejects.append(Reject(slot.name, f"params.{key}", want, params.get(key), "expect-mismatch"))
        if "openings" in slot.expect:
            actual = params.get("openings")
            expect = slot.expect["openings"]
            if not isinstance(actual, list):
                rejects.append(Reject(slot.name, "params.openings", f"{len(expect)} 项", actual, "shape"))
            elif len(actual) != len(expect):
                rejects.append(Reject(slot.name, "params.openings", len(expect), len(actual), "opening-count"))
            else:
                for i, e, matched in _match_openings(expect, actual):
                    if matched is None:
                        rejects.append(Reject(slot.name, f"params.openings[{i}]", e, "缺失或错位", "opening-missing"))
                        continue
                    for k in ("x", "z", "w", "h"):
                        got = _num(matched.get(k))
                        tol = DEFAULT_POS_TOL if k in ("x", "z") else DEFAULT_TOL
                        if got is None:
                            rejects.append(Reject(slot.name, f"params.openings[{i}].{k}", e[k], matched.get(k), "not-a-number"))
                        elif abs(got - float(e[k])) > tol:
                            rejects.append(Reject(slot.name, f"params.openings[{i}].{k}", e[k], matched.get(k), "expect-mismatch"))
        for key in slot.free:
            if key not in params:
                continue
            lo, hi = slot.domain[key]
            got = _num(params.get(key))
            if got is None:
                rejects.append(Reject(slot.name, f"params.{key}", f"{lo}..{hi}", params.get(key), "not-a-number"))
                continue
            if not (lo - 1e-9 <= got <= hi + 1e-9):
                rejects.append(Reject(slot.name, f"params.{key}", f"{lo}..{hi}", params.get(key), "domain"))
        # builder 自带的域（builder 内部还有一层约束，避免声明漏写域时静默放过）
        for key, (lo, hi) in DOMAINS.get(slot.builder, {}).items():
            if key not in params or key in slot.free:
                continue
            got = _num(params.get(key))
            if got is not None and not (lo - 1e-9 <= got <= hi + 1e-9):
                rejects.append(Reject(slot.name, f"params.{key}", f"{lo}..{hi}", params.get(key), "domain"))
    return rejects


@dataclass(frozen=True)
class CheckResult:
    id: str
    ok: bool
    expected: str
    actual: Any
    samples: int
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ok": self.ok, "expected": self.expected, "actual": self.actual, "samples": self.samples, "why": self.why}


def check_requires(spec: dict) -> tuple[str, ...]:
    """本检查**读取哪些 slot 的几何**。这是依赖集，不是坐标系。

    兼容：旧检查只有 `slot` 时，视为"坐标 + 依赖都是它"（v1 语义不变）；
    世界系检查没有 coordinate_slot 时依赖集为空——它只能由覆盖全部活动槽位的祖先履责。
    """
    raw = spec.get("requires_slots")
    if isinstance(raw, list) and raw:
        return tuple(str(s) for s in raw)
    coordinate = str(spec.get("coordinate_slot") or spec.get("slot") or "")
    return (coordinate,) if coordinate else ()


def check_coordinate(spec: dict) -> str:
    """局部系检查用哪个槽位的冻结坐标系定位；空串表示世界系。"""
    if str(spec.get("space", "local")) == "world":
        return ""
    return str(spec.get("coordinate_slot") or spec.get("slot") or "")


def _resolve_region(decl: Declaration, spec: dict, placed: list[PlacedPart]) -> Box:
    raw = spec.get("box")
    if not isinstance(raw, list) or len(raw) != 6:
        raise AssemblyError(f"check {spec.get('id')}: box must be 6 numbers")
    if not all(finite_number(v) for v in raw) or any(raw[i + 3] <= raw[i] for i in range(3)):
        raise AssemblyError(f"check {spec.get('id')}: box must have positive finite extents")
    b = box(*(float(v) for v in raw))
    slot = check_coordinate(spec)
    if not slot:
        return b
    # 坐标系来自冻结声明，与"这个槽位是否已交付"无关：主体包不在时，窗框检查仍能定位
    return world_region(decl, slot, b)


def _scope_parts(spec: dict, placed: list[PlacedPart]) -> list[PlacedPart]:
    """只给本检查**依赖**的几何，避免别的 slot 遮蔽缺陷（例如实心墙填满缺窗框的区域）。"""
    requires = set(check_requires(spec))
    if not requires:
        return placed
    return [p for p in placed if p.slot in requires]


def run_checks(decl: Declaration, placed: list[PlacedPart], samples: int = 512,
              frames: "Declaration | None" = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    for i, spec in enumerate(decl.checks):
        cid = str(spec.get("id") or f"check{i}")
        rule = str(spec.get("rule"))
        scoped = _scope_parts(spec, placed)
        missing = [s for s in check_requires(spec) if not any(p.slot == s for p in placed)]
        if missing:
            # 依赖未交付：不是"通过"，也不是"区域为空"——明确报缺件
            results.append(CheckResult(cid, False, "依赖已交付", f"missing slots: {missing}", 0, str(spec.get("why") or "")))
            continue
        if rule == "runs_along_x":
            runs, n = _runs_along_x(spec, scoped)
            lo, hi = int(spec.get("min_runs", 1)), int(spec.get("max_runs", 10**6))
            ok = n > 0 and lo <= runs <= hi
            results.append(CheckResult(cid, ok, f"{lo}..{hi} 段", runs, n, str(spec.get("why") or "")))
            continue
        try:
            # frames 是**完整**冻结声明：坐标系可以指向本 scope 没交付的 slot（R-C2）
            region = _resolve_region(frames or decl, spec, placed)
        except (AssemblyError, ValueError) as exc:
            results.append(CheckResult(cid, False, "区域可解析", f"无法解析: {exc}", 0, str(spec.get("why") or "")))
            continue
        frac, n = region_fraction(scoped, region, count=samples, offset=i * 131)
        if n == 0:
            # 空区域会让比例恒为 0，而 0 恰好是"空洞"的合格值 → 必须判失败，不能判通过
            results.append(CheckResult(cid, False, "可采样", "0 个样本（区域为空或在场景外）", 0, str(spec.get("why") or "")))
            continue
        if rule == "region_empty":
            thr = float(spec.get("threshold", 0.05))
            results.append(CheckResult(cid, frac < thr, f"<{thr:g}", round(frac, 4), n, str(spec.get("why") or "")))
        elif rule == "region_filled":
            lo = float(spec.get("min", 0.75))
            results.append(CheckResult(cid, frac >= lo, f">={lo:g}", round(frac, 4), n, str(spec.get("why") or "")))
        elif rule == "region_between":
            lo, hi = float(spec.get("min", 0.0)), float(spec.get("max", 1.0))
            results.append(CheckResult(cid, lo <= frac <= hi, f"{lo:g}..{hi:g}", round(frac, 4), n, str(spec.get("why") or "")))
        else:
            results.append(CheckResult(cid, False, "已知规则", f"未知规则 {rule!r}", n, ""))
    return results


def _runs_along_x(spec: dict, placed: list[PlacedPart], steps: int = 240) -> tuple[int, int]:
    y, z = float(spec["y"]), float(spec["z"])
    x0, x1 = float(spec["x0"]), float(spec["x1"])
    idx = SolidIndex(placed)
    prev, runs, n = False, 0, 0
    for i in range(steps):
        x = x0 + (x1 - x0) * i / (steps - 1)
        cur = idx.contains((x, y, z))
        n += 1
        if cur and not prev:
            runs += 1
        prev = cur
    return runs, n


def run_degeneration_checks(decl: Declaration, placed: list[PlacedPart]) -> list[CheckResult]:
    """退化守卫：构件不能是尘/刀片 —— bbox 中位维度不得低于 min_member_m。

    **这不是精度出口**（spec §5.2）：bbox 中位数既不证明最薄杆件，也不证明全局精度，
    它只拦"退化到看不见"的几何。没有 precision 块就不评，而不是恒真。
    """
    if decl.precision is None:
        return []
    floor = decl.precision.min_member_m
    out: list[CheckResult] = []
    for part in placed:
        dims = sorted((part.bbox.x1 - part.bbox.x0, part.bbox.y1 - part.bbox.y0, part.bbox.z1 - part.bbox.z0))
        median = dims[1]
        out.append(CheckResult(
            f"degeneration_guard.{part.slot}", median >= floor,
            f"median_dim >= {floor:g}m", round(median, 4), 0,
            "退化守卫：bbox 中位维度下限，不是精度出口",
        ))
    return out


def evaluate_scope(decl: Declaration, parts: Any, samples: int = 512, require_connectivity: bool = True,
                   frames: "Declaration | None" = None) -> dict[str, Any]:
    """完整判定：先合规、再几何、再精度出口。bundle 场景把 require_connectivity 关掉（子集不强求连通）。"""
    if not decl.checks or samples <= 0:
        return {"ok": False, "stage": "checks", "rejects": [{"slot": "*", "field": "checks", "expected": "non-empty checks and positive samples", "rule": "empty-checks", "actual": "checks and samples must be positive"}], "checks": []}
    rejects = validate_parts(decl, parts)
    if rejects:
        return {"ok": False, "stage": "schema", "rejects": [r.to_dict() for r in rejects], "checks": []}
    try:
        placed = place(decl, parts)
    except AssemblyError as exc:
        return {"ok": False, "stage": "assembly", "rejects": [{"slot": "*", "field": "assembly", "expected": "可装配",
                                                               "actual": str(exc), "rule": "assembly"}], "checks": []}
    checks = run_checks(decl, placed, samples=samples, frames=frames)
    if require_connectivity:
        conn = connectivity(placed)
        checks.append(CheckResult("assembly.connectivity", bool(conn["single_component"]), "one solid-contact component", conn, 0))
    checks.extend(run_degeneration_checks(decl, placed))
    return {
        "ok": all(c.ok for c in checks),
        "stage": "checks",
        "rejects": [c.to_dict() for c in checks if not c.ok],
        "checks": [c.to_dict() for c in checks],
        "parts": len(placed),
    }


def evaluate(decl: Declaration, parts: Any, samples: int = 512) -> dict[str, Any]:
    """完整场景判定：全部槽位 + 全部 checks + 强制连通 + 退化守卫。"""
    return evaluate_scope(decl, parts, samples=samples, require_connectivity=True)
