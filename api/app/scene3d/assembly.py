"""装配：按 mount 把每个构件摆到世界系，给出世界盒组 / AABB / 连通性。

连通性判据（V2/V3 的可执行形式）：两构件的世界 AABB 交叠体积 > 0 即视为"搭接"；
整棵树必须是一张单连通图，且不允许任何孤立件。
失效方向：读不出构件几何（builder 抛错）→ **阻断**；图建不起来（0 个构件）→ 阻断。
"""

from __future__ import annotations

from dataclasses import dataclass

from .boxes import Box, aabb, box, overlap_volume, transform
from .builders import build
from .declaration import Declaration


@dataclass(frozen=True)
class PlacedPart:
    slot: str
    builder: str
    local: tuple[Box, ...]
    world: tuple[Box, ...]
    bbox: Box

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot,
            "builder": self.builder,
            "boxes": len(self.world),
            "bbox": [round(v, 3) for v in self.bbox],
        }


class AssemblyError(ValueError):
    """装配失败（几何不可构造）。出现即阻断。"""


def place(decl: Declaration, parts: list[dict]) -> list[PlacedPart]:
    out: list[PlacedPart] = []
    for part in parts:
        slot = decl.by_name(str(part.get("slot") or ""))
        if slot is None:
            raise AssemblyError(f"part for undeclared slot {part.get('slot')!r}")
        try:
            local = build(part)
        except ValueError as exc:
            raise AssemblyError(f"{slot.name}: {exc}") from exc
        world = tuple(transform(b, slot.mount.origin, slot.mount.rot) for b in local)
        bb = aabb(world)
        if bb is None:
            raise AssemblyError(f"{slot.name}: empty geometry")
        out.append(PlacedPart(slot=slot.name, builder=slot.builder, local=local, world=world, bbox=bb))
    if not out:
        raise AssemblyError("assembly has no parts")
    return out


def scene_bbox(placed: list[PlacedPart]) -> Box:
    boxes = [p.bbox for p in placed]
    bb = aabb(boxes)
    if bb is None:
        raise AssemblyError("scene bbox undefined")
    return bb


def _mesh_overlap(a: Box, b: Box, eps: float = 1e-6) -> bool:
    dx = min(a.x1, b.x1) - max(a.x0, b.x0)
    dy = min(a.y1, b.y1) - max(a.y0, b.y0)
    dz = min(a.z1, b.z1) - max(a.z0, b.z0)
    # Solid overlap or positive-area face contact; edge/point contact is not a mount.
    extents = (dx, dy, dz)
    return all(v >= -eps for v in extents) and sum(v > eps for v in extents) >= 2


def connectivity(placed: list[PlacedPart]) -> dict[str, object]:
    """实心盒接触图；AABB 仅用于粗筛，不能把洞内悬空构件误判为连接。"""
    n = len(placed)
    adj: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _mesh_overlap(placed[i].bbox, placed[j].bbox) and any(
                _mesh_overlap(a, b) for a in placed[i].world for b in placed[j].world
            ):
                adj[i].add(j)
                adj[j].add(i)
    isolated = [placed[i].slot for i in range(n) if not adj[i]]
    seen: set[int] = set()
    stack = [0] if n else []
    while stack:
        k = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        stack.extend(adj[k] - seen)
    return {
        "parts": n,
        "reachable_from_first": len(seen),
        "isolated": isolated,
        "single_component": len(seen) == n and (n == 1 or not isolated) and n > 0,
    }


def total_volume(placed: list[PlacedPart]) -> float:
    """各构件体积之和（重叠部分会重复计）。只用于"有没有几何"的粗判与报告。"""
    return sum(sum(b.volume for b in p.world) for p in placed)


def pierce_depth(placed: list[PlacedPart], a: str, b: str) -> float:
    """两个构件 AABB 的最小穿透深度（米）。0 表示只是相切或分离。"""
    pa = next(p for p in placed if p.slot == a)
    pb = next(p for p in placed if p.slot == b)
    ov = overlap_volume(pa.bbox, pb.bbox)
    if ov <= 0:
        return 0.0
    return min(
        min(pa.bbox.x1, pb.bbox.x1) - max(pa.bbox.x0, pb.bbox.x0),
        min(pa.bbox.y1, pb.bbox.y1) - max(pa.bbox.y0, pb.bbox.y0),
        min(pa.bbox.z1, pb.bbox.z1) - max(pa.bbox.z0, pb.bbox.z0),
    )


def world_region(decl: Declaration, slot: str, local: Box) -> Box:
    """把某槽位局部系里的盒子换算到世界系（特征校验用）。"""
    s = decl.by_name(slot)
    if s is None:
        raise AssemblyError(f"world_region: undeclared slot {slot!r}")
    return transform(local, s.mount.origin, s.mount.rot)


def expand(b: Box, d: float) -> Box:
    return box(b.x0 - d, b.y0 - d, b.z0 - d, b.x1 + d, b.y1 + d, b.z1 + d)
