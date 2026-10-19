"""装配体之间的相似度度量（IoU / 越界 / 规模）。

为什么不用"细节密度 D"：上一轮实测里 D 对"有没有窗棂"只差 2%，几乎不可分，
它唯一可靠的用途是防"把一切做成实心块"。这里改报**构件数 / 盒数 / 体积**这些
直接可核对的事实，把"细化"的判定交给特征校验清单（checks），不再用一个模糊的密度指数。

采样是确定性的（Halton），同一份输入必须给出同一个 IoU —— 否则门禁会在两次运行间抖动。
"""

from __future__ import annotations

from .assembly import PlacedPart, scene_bbox
from .boxes import Box, aabb, box, sample_points
from .solid import SolidIndex

TAU = 0.25  # 允许偏离目标的容差带（米）


def _union_bbox(a: list[PlacedPart], b: list[PlacedPart]) -> Box:
    boxes = [scene_bbox(a), scene_bbox(b)]
    bb = aabb(boxes)
    assert bb is not None
    return bb


def _expanded_index(placed: list[PlacedPart], tau: float) -> SolidIndex:
    from .assembly import PlacedPart as PP

    grown = [
        PP(slot=p.slot, builder=p.builder, local=p.local, world=tuple(box(b.x0 - tau, b.y0 - tau, b.z0 - tau, b.x1 + tau, b.y1 + tau, b.z1 + tau) for b in p.world), bbox=box(p.bbox.x0 - tau, p.bbox.y0 - tau, p.bbox.z0 - tau, p.bbox.x1 + tau, p.bbox.y1 + tau, p.bbox.z1 + tau))
        for p in placed
    ]
    return SolidIndex(grown)


def compare(candidate: list[PlacedPart], target: list[PlacedPart], samples: int = 12000, tau: float = TAU) -> dict:
    """candidate 相对 target 的 IoU / 越界比例 / 规模。"""
    bb = _union_bbox(candidate, target)
    ca, ct = SolidIndex(candidate), SolidIndex(target)
    env = _expanded_index(target, tau)
    pts = sample_points((bb.x0, bb.y0, bb.z0), (bb.x1, bb.y1, bb.z1), samples)
    if not pts:
        return {"ok": False, "why": "0 个采样点（场景盒退化）"}

    inter = only_a = only_t = a_total = 0
    spill = 0
    for p in pts:
        ia = ca.contains(p)
        it = ct.contains(p)
        if ia:
            a_total += 1
        if ia and it:
            inter += 1
        elif ia:
            only_a += 1
            if not env.contains(p):
                spill += 1
        elif it:
            only_t += 1
    union = inter + only_a + only_t
    return {
        "ok": True,
        "iou": round(inter / union, 5) if union else 1.0,
        "spill": round(spill / a_total, 5) if a_total else 0.0,
        "gap": round(only_t / (inter + only_t), 5) if (inter + only_t) else 0.0,
        "samples": len(pts),
        "candidate": summary(candidate),
        "target": summary(target),
    }


def summary(placed: list[PlacedPart]) -> dict:
    boxes = sum(len(p.world) for p in placed)
    vol = sum(b.volume for p in placed for b in p.world)
    bb = scene_bbox(placed)
    return {
        "parts": len(placed),
        "boxes": boxes,
        "volume": round(vol, 3),
        "bbox": [round(v, 3) for v in bb],
    }
