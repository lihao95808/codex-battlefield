"""轴对齐盒体原语与确定性采样。

本模块刻意不依赖 numpy / trimesh / manifold3d：
战场 API 的依赖表里没有数值栈，而"能不能判 3D"不该以装一个科学计算栈为前提。
几何约定：**一个构件的实体就是一组轴对齐实心盒**（洞在 builder 里被分解成盒带，不做 CSG）。
这样一来
    point ∈ 实体  <=>  any(box.contains(point))
    volume(实体)  =   analytically exact per box，重叠部分由采样度量吸收
    导出 GLB      =   每盒 12 个三角形，无需三角化库
三条都变成纯 Python 的常数级操作。
"""

from __future__ import annotations

from typing import Iterable, NamedTuple

EPS = 1e-9


class Box(NamedTuple):
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float

    def normalized(self) -> "Box":
        return Box(
            min(self.x0, self.x1), min(self.y0, self.y1), min(self.z0, self.z1),
            max(self.x0, self.x1), max(self.y0, self.y1), max(self.z0, self.z1),
        )

    def contains(self, p: tuple[float, float, float]) -> bool:
        return (
            self.x0 - EPS <= p[0] <= self.x1 + EPS
            and self.y0 - EPS <= p[1] <= self.y1 + EPS
            and self.z0 - EPS <= p[2] <= self.z1 + EPS
        )

    @property
    def volume(self) -> float:
        b = self.normalized()
        return max(0.0, b.x1 - b.x0) * max(0.0, b.y1 - b.y0) * max(0.0, b.z1 - b.z0)

    @property
    def center(self) -> tuple[float, float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2, (self.z0 + self.z1) / 2)


def box(*coords: float) -> Box:
    return Box(*coords).normalized()


def aabb(boxes: Iterable[Box]) -> Box | None:
    items = list(boxes)
    if not items:
        return None
    return Box(
        min(b.x0 for b in items), min(b.y0 for b in items), min(b.z0 for b in items),
        max(b.x1 for b in items), max(b.y1 for b in items), max(b.z1 for b in items),
    )


def overlap_volume(a: Box, b: Box) -> float:
    dx = min(a.x1, b.x1) - max(a.x0, b.x0)
    dy = min(a.y1, b.y1) - max(a.y0, b.y0)
    dz = min(a.z1, b.z1) - max(a.z0, b.z0)
    if dx <= EPS or dy <= EPS or dz <= EPS:
        return 0.0
    return dx * dy * dz


def solid_contains(boxes: tuple[Box, ...], p: tuple[float, float, float]) -> bool:
    return any(b.contains(p) for b in boxes)


def transform(b: Box, origin: tuple[float, float, float], rot: int) -> Box:
    """局部系 → 父体系的轴对齐变换（只支持绕 Z 的 0/90/180/270，正是 mount 契约允许的范围）。

    90 度旋转把矩形转到轴对齐，所以不需要矩阵，也不会引入浮点倾斜。
    """
    rot %= 360
    if rot == 0:
        q = (b.x0, b.y0, b.x1, b.y1)
    elif rot == 90:  # (x, y) -> (-y, x)
        q = (-b.y1, b.x0, -b.y0, b.x1)
    elif rot == 180:  # (x, y) -> (-x, -y)
        q = (-b.x1, -b.y1, -b.x0, -b.y0)
    elif rot == 270:  # (x, y) -> (y, -x)
        q = (b.y0, -b.x1, b.y1, -b.x0)
    else:
        raise ValueError(f"mount.rot must be 0/90/180/270, got {rot}")
    ox, oy, oz = origin
    return box(q[0] + ox, q[1] + oy, b.z0 + oz, q[2] + ox, q[3] + oy, b.z1 + oz)


# --------------------------------------------------------------- 采样


def _halton(index: int, base: int) -> float:
    f, r = 1.0, 0.0
    while index > 0:
        f /= base
        r += f * (index % base)
        index //= base
    return r


def sample_points(lo: tuple[float, float, float], hi: tuple[float, float, float], count: int, offset: int = 0):
    """在盒内取确定性准随机点（Halton 2/3/5）。

    确定性是硬要求：同一份输入必须给出同一个 IoU，否则门禁会在两次运行之间抖动。
    offset 让不同区域的样本互不相同（避免同一组点在两块区域上重复命中同一条格线）。
    """
    if count <= 0:
        return []
    pts = []
    for i in range(1, count + 1):
        n = i + offset
        pts.append((
            lo[0] + (hi[0] - lo[0]) * _halton(n, 2),
            lo[1] + (hi[1] - lo[1]) * _halton(n, 3),
            lo[2] + (hi[2] - lo[2]) * _halton(n, 5),
        ))
    return pts


def inside_fraction(
    boxes: tuple[Box, ...], region: Box, count: int = 512, offset: int = 0
) -> tuple[float, int]:
    """region 内落在实体里的点占比。返回 (比例, 有效样本数)。

    有效样本数为 0 时调用方必须判失败（而不是把 0 当"干净"）——见 checks 的失效方向说明。
    """
    b = region.normalized()
    pts = sample_points((b.x0, b.y0, b.z0), (b.x1, b.y1, b.z1), count, offset)
    if not pts:
        return 0.0, 0
    hit = sum(1 for p in pts if solid_contains(boxes, p))
    return hit / len(pts), len(pts)
