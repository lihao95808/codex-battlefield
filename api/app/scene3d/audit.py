"""冻结期审计：每项检查的负样本（R-C8）与合法参数域相容性（R-C7）。

为什么必须在冻结期做，而不是靠"写测试的时候记得覆盖"：

- **R-C8 负样本**：一个阈值写成 0（`region_filled`）或 1（`region_empty`）的检查永远通过。
  它和"检查真的通过了"在输出上完全一样，只有人为制造反例才能区分。
- **R-C7 域相容**：检查的采样区域可能假设了某种具体实现（例如"整条线脚都是满外凸"），
  但声明允许的自由参数可以合法地取到别的形态。那样**合法实现会被判失败**，
  而模型会以为是自己做错了，反复修一个没有错的东西。

两条都不产生新几何，只复用已冻结的声明与确定性 builder。
失效方向：审计自身出错 → 视为不通过（不能因为"审计跑挂了"就当检查没问题）。
"""

from __future__ import annotations

import itertools
from typing import Any, Iterator

from .assembly import PlacedPart, place
from .boxes import box
from .checks import _resolve_region, check_requires, run_checks
from .declaration import Declaration, reference_parts

AUDIT_SAMPLES = 64
FAR_AWAY = 1000.0
MUTATION_SIZE = 0.01


def _single_check_declaration(decl: Declaration, check: dict[str, Any]) -> Declaration:
    return Declaration(
        name=decl.name, slots=decl.slots, size_m=decl.size_m, notes=decl.notes,
        checks=(check,), precision=None,
    )


def _parts_with(decl: Declaration, overrides: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    parts = reference_parts(decl)
    for part in parts:
        override = overrides.get(str(part["slot"]))
        if override:
            params = dict(part["params"])
            params.update(override)
            part["params"] = params
    return parts


def _replace_geometry(placed: list[PlacedPart], slots: set[str], replacement) -> list[PlacedPart]:
    """把指定 slot 的几何换掉，但**保留 slot 本身**（避免退化成"缺件"错误）。"""
    out: list[PlacedPart] = []
    for part in placed:
        if part.slot in slots:
            world = (replacement,)
            out.append(PlacedPart(slot=part.slot, builder=part.builder, local=part.local,
                                  world=world, bbox=replacement))
        else:
            out.append(part)
    return out


def negative_controls(decl: Declaration, samples: int = AUDIT_SAMPLES) -> list[dict[str, Any]]:
    """每项检查必须**双向**可区分：参考实现通过，且违反几何时失败。

    只有"反例下失败"是不够的——一个永久失败的检查同样无法区分对错，而且比恒真更糟：
    它会把每个合法实现都判成缺陷。所以先验正向，再验反向。

    反例必须**就地留下少量材料**（把几何搬到很远会变成"0 样本"，
    那时失败的理由是"采样不到"，不是"几何不对"，会掩盖恒真缺陷）。
    """
    placed = place(decl, reference_parts(decl))
    baseline = {r.id: r for r in run_checks(decl, placed, samples=samples, frames=decl)}
    problems: list[dict[str, Any]] = []
    for check in decl.checks:
        cid = str(check.get("id") or "")
        rule = str(check.get("rule"))
        requires = set(check_requires(check))
        if not requires:
            problems.append({"id": cid, "issue": "no-dependency", "detail": "检查没有声明 requires_slots"})
            continue
        positive = baseline.get(cid)
        if positive is None or not positive.ok:
            actual = None if positive is None else positive.actual
            problems.append({
                "id": cid, "issue": "reference-fails",
                "detail": f"参考实现下检查就不通过（actual={actual}），它会把合法实现全判成缺陷",
            })
            continue
        try:
            if rule == "region_empty":
                mutation = _resolve_region(decl, check, placed)
                mutated = _replace_geometry(placed, requires, mutation)
            elif rule == "runs_along_x":
                # 该规则沿一条线数段数，没有 box：把材料缩到线上的一个点，段数必然降到 1
                half = MUTATION_SIZE / 2
                x = (float(check["x0"]) + float(check["x1"])) / 2
                y, z = float(check["y"]), float(check["z"])
                mutation = box(x - half, y - half, z - half, x + half, y + half, z + half)
                mutated = _replace_geometry(placed, requires, mutation)
            else:
                center = _resolve_region(decl, check, placed).center
                half = MUTATION_SIZE / 2
                mutation = box(center[0] - half, center[1] - half, center[2] - half,
                               center[0] + half, center[1] + half, center[2] + half)
                mutated = _replace_geometry(placed, requires, mutation)
            results = run_checks(_single_check_declaration(decl, check), mutated,
                                 samples=samples, frames=decl)
        except Exception as exc:  # noqa: BLE001 - 审计自身出错按不通过处理
            problems.append({"id": cid, "issue": "audit-error", "detail": f"{type(exc).__name__}: {exc}"})
            continue
        if not results:
            problems.append({"id": cid, "issue": "not-run", "detail": "检查没有产生结果"})
            continue
        negative = results[0]
        if negative.ok:
            problems.append({
                "id": cid, "issue": "always-true",
                "detail": f"反例几何下仍然通过（actual={negative.actual}），该检查无法区分对错",
            })
        elif negative.samples == 0:
            problems.append({
                "id": cid, "issue": "unmeasurable",
                "detail": "反例下 0 个采样点：失败理由不是几何不对，无法证明检查有效",
            })
    return problems


def _corner_values(lo: float, hi: float) -> list[tuple[str, float]]:
    """域角点。整数域只取整数值：否则 6.5 这类"根本不是合法参数"会伪装成审计失败。"""
    integral = float(lo).is_integer() and float(hi).is_integer()
    if integral:
        values = sorted({int(lo), int(round((lo + hi) / 2)), int(hi)})
        return [("lo", values[0]), ("mid", values[len(values) // 2]), ("hi", values[-1])]
    return [("lo", lo), ("mid", (lo + hi) / 2), ("hi", hi)]


MAX_COMBINATION_AXES = 4


def domain_variants(decl: Declaration) -> Iterator[tuple[str, dict[str, dict[str, Any]]]]:
    """枚举自由参数域的角点。

    只扫"一次动一个参数"是不够的：实测 `sink` 取高值、`bar` 取低值会造出埋在墙里的窗框，
    而单独取高或单独取低都正常——**危险的在组合上**。所以对 ≤4 个自由参数的槽位枚举
    全部 2^k 组合；更多参数时退化为逐参数角点加两两交叉。
    """
    for slot in decl.slots:
        names = list(slot.free)
        if not names:
            continue
        corners = {n: [value for _, value in _corner_values(*slot.domain[n])] for n in names}
        if len(names) <= MAX_COMBINATION_AXES:
            combos = list(itertools.product(*(corners[n] for n in names)))
            labelled = [dict(zip(names, combo, strict=False)) for combo in combos]
        else:
            labelled = [{n: corners[n][0] for n in names}, {n: corners[n][-1] for n in names}]
            for a, b in itertools.combinations(names, 2):
                for va, vb in ((corners[a][0], corners[b][-1]), (corners[a][-1], corners[b][0])):
                    base = {n: corners[n][0] for n in names}
                    base[a], base[b] = va, vb
                    labelled.append(base)
        for mapping in labelled:
            label = f"{slot.name}:" + ",".join(f"{k}={mapping[k]}" for k in names)
            yield label, {slot.name: mapping}

    def _unused() -> Iterator[tuple[str, dict[str, dict[str, Any]]]]:
        # 保留生成器契约（函数体必须是生成器），不会被调用
        yield ("", {})


def _affected_checks(decl: Declaration, slot: str) -> tuple[dict[str, Any], ...]:
    """只跑与该 slot 有关的检查：依赖包含它，或坐标系指向它。"""
    out = []
    for check in decl.checks:
        if slot in check_requires(check) or str(check.get("coordinate_slot") or "") == slot:
            out.append(check)
    return tuple(out)


def domain_compatibility(decl: Declaration, samples: int = AUDIT_SAMPLES) -> list[dict[str, Any]]:
    """自由参数取合法角点时，受影响的检查必须仍然通过（R-C7）。"""
    problems: list[dict[str, Any]] = []
    for label, overrides in domain_variants(decl):
        slot = next(iter(overrides))
        affected = _affected_checks(decl, slot)
        if not affected:
            continue
        parts = _parts_with(decl, overrides)
        try:
            placed = place(decl, parts)
            scoped = Declaration(
                name=decl.name, slots=decl.slots, size_m=decl.size_m, notes=decl.notes,
                checks=affected, precision=None,
            )
            results = run_checks(scoped, placed, samples=samples, frames=decl)
        except Exception as exc:  # noqa: BLE001
            problems.append({"variant": label, "issue": "audit-error", "detail": f"{type(exc).__name__}: {exc}"})
            continue
        for result in results:
            if not result.ok:
                problems.append({
                    "variant": label, "id": result.id, "issue": "legal-value-fails",
                    "detail": f"合法参数下检查失败（actual={result.actual}, expected={result.expected}）",
                })
    return problems


def audit_declaration(decl: Declaration, samples: int = AUDIT_SAMPLES) -> dict[str, Any]:
    """冻结期审计总入口。任何 problem 都意味着声明/检查本身有问题，不是实现的问题。"""
    negative = negative_controls(decl, samples=samples)
    domain = domain_compatibility(decl, samples=samples)
    return {
        "ok": not negative and not domain,
        "checks": len(decl.checks),
        "slots": len(decl.slots),
        "negative_problems": negative,
        "domain_problems": domain,
        "samples": samples,
    }


__all__ = ["audit_declaration", "domain_compatibility", "domain_variants", "negative_controls"]
