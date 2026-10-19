"""装配体的空间索引：把"某点是否在实体里"从 O(全部盒) 降到 O(附近盒)。

为什么需要：场景约 1300 个盒、28 条特征校验、每条 512 个采样点。朴素实现要 1800 万次
盒内测试（纯 Python 约 20–40 秒），足以让校验器从"每次都跑"退化成"跑不动就跳过"——
那正是门禁烂掉的典型路径。分桶后单点是几十次比较，整体回到秒级。
"""

from __future__ import annotations

from .assembly import PlacedPart, scene_bbox
from .boxes import Box, inside_fraction, solid_contains

DIV = 12  # 每个轴切多少桶。12^3 = 1728 桶，对 14m 场景约 1.2m/桶


class SolidIndex:
    """把一组构件的世界盒压平到粗格上，支持点内测试与区域占比。"""

    def __init__(self, placed: list[PlacedPart], div: int = DIV):
        self.bbox = scene_bbox(placed)
        self.div = div
        self.boxes: list[Box] = []
        owner: list[int] = []
        for pi, part in enumerate(placed):
            for b in part.world:
                self.boxes.append(b)
                owner.append(pi)
        span = (self.bbox.x1 - self.bbox.x0, self.bbox.y1 - self.bbox.y0, self.bbox.z1 - self.bbox.z0)
        self.step = tuple(max(s / div, 1e-6) for s in span)
        self.buckets: dict[tuple[int, int, int], list[int]] = {}
        for bi, b in enumerate(self.boxes):
            for key in self._cells(b):
                self.buckets.setdefault(key, []).append(bi)

    def _cell(self, p: tuple[float, float, float]) -> tuple[int, int, int]:
        return (
            min(int((p[0] - self.bbox.x0) / self.step[0]), self.div - 1),
            min(int((p[1] - self.bbox.y0) / self.step[1]), self.div - 1),
            min(int((p[2] - self.bbox.z0) / self.step[2]), self.div - 1),
        )

    def _cells(self, b: Box):
        lo = self._cell((max(b.x0, self.bbox.x0), max(b.y0, self.bbox.y0), max(b.z0, self.bbox.z0)))
        hi = self._cell((min(b.x1, self.bbox.x1 - 1e-9), min(b.y1, self.bbox.y1 - 1e-9), min(b.z1, self.bbox.z1 - 1e-9)))
        for ix in range(lo[0], hi[0] + 1):
            for iy in range(lo[1], hi[1] + 1):
                for iz in range(lo[2], hi[2] + 1):
                    yield (ix, iy, iz)

    def contains(self, p: tuple[float, float, float]) -> bool:
        if not (self.bbox.x0 <= p[0] <= self.bbox.x1 and self.bbox.y0 <= p[1] <= self.bbox.y1 and self.bbox.z0 <= p[2] <= self.bbox.z1):
            return False
        for bi in self.buckets.get(self._cell(p), ()):
            if self.boxes[bi].contains(p):
                return True
        return False

    def fraction(self, region: Box, count: int = 512, offset: int = 0) -> tuple[float, int]:
        from .boxes import sample_points

        b = region.normalized()
        pts = sample_points((b.x0, b.y0, b.z0), (b.x1, b.y1, b.z1), count, offset)
        if not pts:
            return 0.0, 0
        hit = sum(1 for p in pts if self.contains(p))
        return hit / len(pts), len(pts)


def region_boxes(placed: list[PlacedPart], region: Box) -> tuple[Box, ...]:
    """只取与 region 相交的构件的盒（用于局部区域的小规模精确判定）。"""
    r = region.normalized()
    out: list[Box] = []
    for part in placed:
        b = part.bbox
        if b.x1 <= r.x0 or b.x0 >= r.x1 or b.y1 <= r.y0 or b.y0 >= r.y1 or b.z1 <= r.z0 or b.z0 >= r.z1:
            continue
        out.extend(part.world)
    return tuple(out)


def region_fraction(placed: list[PlacedPart], region: Box, count: int = 512, offset: int = 0) -> tuple[float, int]:
    boxes = region_boxes(placed, region)
    if not boxes:
        return 0.0, 0
    return inside_fraction(boxes, region, count, offset)


__all__ = ["SolidIndex", "region_boxes", "region_fraction", "solid_contains"]
