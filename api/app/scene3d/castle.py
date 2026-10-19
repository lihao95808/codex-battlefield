"""内置场景：石堡（10m × 8m × 7m 墙 + 四角方塔 + 雉堞 + 门 + 窗棂 + 腰线）。

这里只放**声明数据**，不含任何造型代码 —— 声明是 L0 产物，worker 不能改。
特征校验（checks）与声明一起给出，而不是藏在代码里的隐含约定：
这样"到底要验什么"是可读、可 diff、可被 planner 复用的数据。
"""

from __future__ import annotations

from .declaration import DEFAULT_POS_TOL, DEFAULT_TOL, Declaration, Mount, Slot

W, D, H, T = 10.0, 8.0, 7.0, 0.6
H_EW = round(H - 0.012, 3)  # 东/西墙低 12mm：避免与南北墙顶面共面（V4 的构造性规避）

WIN_SILL, WIN_H, WIN_W = 2.2, 1.6, 1.0
DOOR_W, DOOR_H = 1.6, 2.6
MOULD = {"x0": 0.3, "x1": 9.7, "z": 4.2, "protrude": 0.10, "band_h": 0.18}
TOWER_SIZE, TOWER_H = 3.0, 8.6
TOWER_CENTERS = {"towerSW": (0.3, 0.3), "towerSE": (9.7, 0.3), "towerNE": (9.7, 7.7), "towerNW": (0.3, 7.7)}

WALLS = {
    "wallS": {"w": W, "h": H, "mount": Mount((0.0, 0.0, 0.0), 0), "wins": (2.6, 7.4), "door": 5.0},
    "wallN": {"w": W, "h": H, "mount": Mount((10.0, 8.0, 0.0), 180), "wins": (2.6, 7.4), "door": None},
    "wallW": {"w": D, "h": H_EW, "mount": Mount((0.6, 0.0, 0.0), 90), "wins": (4.0,), "door": None},
    "wallE": {"w": D, "h": H_EW, "mount": Mount((9.4, 8.0, 0.0), 270), "wins": (4.0,), "door": None},
}

BATTLE = {
    "battleS": {"mount": Mount((0.0, 0.0, H), 0), "length": W},
    "battleN": {"mount": Mount((10.0, 8.0, H), 180), "length": W},
    "battleW": {"mount": Mount((0.35, 0.0, H_EW), 90), "length": D},
    "battleE": {"mount": Mount((9.65, 8.0, H_EW), 270), "length": D},
}


def _win_box(cx: float) -> tuple[float, float, float, float]:
    return (cx - WIN_W / 2, WIN_SILL, cx + WIN_W / 2, WIN_SILL + WIN_H)


def _slots() -> list[Slot]:
    slots: list[Slot] = []

    for name, spec in WALLS.items():
        openings = [{"x": b[0], "z": b[1], "w": WIN_W, "h": WIN_H} for b in (_win_box(c) for c in spec["wins"])]
        if spec["door"] is not None:
            openings.append({"x": spec["door"] - DOOR_W / 2, "z": 0.0, "w": DOOR_W, "h": DOOR_H})
        slots.append(
            Slot(
                name=name,
                builder="wall_panel",
                mount=spec["mount"],
                expect={"w": spec["w"], "h": spec["h"], "t": T, "openings": openings},
            )
        )

    for name, spec in BATTLE.items():
        slots.append(
            Slot(
                name=name,
                builder="battlement",
                mount=spec["mount"],
                expect={"length": spec["length"], "depth": 0.35, "merlon_w": 0.75, "gap": 0.55, "merlon_h": 0.85, "y0": 0.0},
                free=("base_h",),
                domain={"base_h": (0.18, 0.40)},
            )
        )

    for name, (cx, cy) in TOWER_CENTERS.items():
        slots.append(
            Slot(
                name=name,
                builder="tower",
                mount=Mount((round(cx - TOWER_SIZE / 2, 3), round(cy - TOWER_SIZE / 2, 3), 0.0), 0),
                expect={"size": TOWER_SIZE, "height": TOWER_H},
                free=("tiers", "tier_h"),
                domain={"tiers": (2, 6), "tier_h": (0.3, 0.7)},
            )
        )

    slots.append(
        Slot(
            name="doorS",
            builder="door_leaf",
            mount=Mount((round(5.0 - DOOR_W / 2, 3), 0.0, 0.0), 0),
            expect={"w": DOOR_W, "h": DOOR_H},
            free=("t", "planks", "sink"),
            domain={"t": (0.08, 0.25), "planks": (3, 10), "sink": (0.01, 0.12)},
        )
    )

    slots.append(
        Slot(
            name="mouldS",
            builder="moulding",
            mount=Mount((0.0, 0.0, 0.0), 0),
            expect={**MOULD, "t": T},
            free=("steps",),
            domain={"steps": (1, 2)},
        )
    )

    for name, spec in WALLS.items():
        for i, cx in enumerate(spec["wins"]):
            slots.append(
                Slot(
                    name=f"{name}.frame{i}",
                    builder="lattice",
                    mount=spec["mount"],
                    expect={"cx": cx, "cz": WIN_SILL + WIN_H / 2, "w": WIN_W, "h": WIN_H, "t": T},
                    free=("sink", "bar", "mullions", "rails"),
                    # sink 必须小于 bar：否则框料整根埋在墙里、碰不到洞口（合法却无意义）
                    domain={"sink": (0.01, 0.03), "bar": (0.04, 0.10), "mullions": (0, 4), "rails": (0, 3)},
                )
            )
    return slots


def _checks() -> list[dict]:
    """全部是数据：id / rule / 区域 / 阈值。

    `coordinate_slot` 只提供冻结坐标系；`requires_slots` 才是本检查读取的几何。
    两者分开是必须的：窗框检查借墙的坐标系定位，但只能读窗框的几何——
    否则实心墙会把"缺窗框"填满，检查变成恒真。
    """
    checks: list[dict] = []

    for name, spec in WALLS.items():
        for i, cx in enumerate(spec["wins"]):
            x0, z0, x1, z1 = _win_box(cx)
            checks.append({
                "id": f"{name}.win{i}.aperture", "rule": "region_empty",
                "coordinate_slot": name, "requires_slots": [name],
                "box": [x0 + 0.12, 0.05, z0 + 0.12, x1 - 0.12, 0.15, z1 - 0.12], "threshold": 0.05,
                "why": "窗洞必须挖穿（后侧切片应为空）",
            })
            frame_slot = f"{name}.frame{i}"
            # 量**洞口边缘的框料**而不是"中部有几根棂"：后者会随 mullions/rails/bar 的合法取值变化，
            # 把"没做棂"这种合法设计判成缺陷。边缘框料对全部分布域都必然存在。
            checks.append({
                "id": f"{name}.win{i}.frame", "rule": "region_filled",
                "coordinate_slot": name, "requires_slots": [frame_slot],
                "box": [x0 - 0.008, T / 2 - 0.03, z0 + 0.10, x0 + 0.002, T / 2 + 0.03, z1 - 0.10],
                "min": 0.90,
                "why": "洞口边缘必须有窗框料（只读窗框几何；墙只提供坐标）",
            })
        if spec["door"] is not None:
            cx = spec["door"]
            checks.append({
                "id": f"{name}.door.aperture", "rule": "region_empty",
                "coordinate_slot": name, "requires_slots": [name],
                "box": [cx - DOOR_W / 2 + 0.15, 0.45, 0.15, cx + DOOR_W / 2 - 0.15, 0.55, DOOR_H - 0.15],
                "threshold": 0.05, "why": "门洞必须在门扇之后被挖穿",
            })

    checks.append({
        "id": "doorS.leaf", "rule": "region_filled", "space": "world", "requires_slots": ["doorS"],
        # 门板厚度 t 的合法下界是 0.08；取样带必须落在最薄门板内部，否则"薄门"被判缺失
        "box": [5.0 - DOOR_W / 2 + 0.05, 0.02, 0.20, 5.0 + DOOR_W / 2 - 0.05, 0.06, DOOR_H - 0.15],
        "min": 0.75, "why": "门洞位置必须真的有门扇",
    })
    checks.append({
        "id": "mouldS.band", "rule": "region_filled", "space": "world", "requires_slots": ["mouldS"],
        # 只量**外层台阶**：两级线脚的上半层故意只有 0.55 倍外凸，量整条带会把设计当成缺陷
        "box": [MOULD["x0"] + 0.10, T + 0.02, MOULD["z"] + 0.02, MOULD["x1"] - 0.10, T + MOULD["protrude"] - 0.01,
                MOULD["z"] + MOULD["band_h"] / 2 - 0.01],
        "min": 0.85, "why": "腰线外层台阶必须真的外凸（只读线脚几何）",
    })
    for name, (cx, cy) in TOWER_CENTERS.items():
        checks.append({
            "id": f"{name}.body", "rule": "region_filled", "space": "world", "requires_slots": [name],
            "box": [cx - 1.0, cy - 1.0, TOWER_H - 0.6, cx + 1.0, cy + 1.0, TOWER_H - 0.2],
            "min": 0.90, "why": "塔身必须存在",
        })
        checks.append({
            "id": f"{name}.cap", "rule": "region_filled", "space": "world", "requires_slots": [name],
            "box": [cx - 1.2, cy - 1.2, TOWER_H + 0.35, cx + 1.2, cy + 1.2, TOWER_H + 0.75],
            "min": 0.15, "why": "塔顶收分层必须存在",
        })
    for name, spec in BATTLE.items():
        checks.append({
            "id": f"{name}.base", "rule": "region_filled",
            "coordinate_slot": name, "requires_slots": [name],
            "box": [0.4, 0.02, 0.02, spec["length"] - 0.4, 0.30, 0.16],
            "min": 0.50, "why": "雉堞基座必须存在",
        })
    checks.append({
        "id": "battleS.merlons", "rule": "runs_along_x", "space": "world", "requires_slots": ["battleS"],
        "y": 0.15, "z": H + 0.70, "x0": -0.2, "x1": W + 0.2, "min_runs": 5, "max_runs": 10,
        "why": "垛口段数必须落在典型范围（既不能没有，也不能密到糊成一片）",
    })
    return checks


def castle_declaration(with_precision: bool = False) -> Declaration:
    precision = None
    if with_precision:
        from .declaration import Precision

        # δ=5cm（窗棂杆件级别）；最薄构件不小于 5cm；槽位上限=20（不许扩场景）；深度上限 2
        precision = Precision(delta_world_m=0.05, min_member_m=0.05, slot_ceiling=20, max_depth=2)
    return Declaration(
        name="castle",
        slots=tuple(_slots()),
        size_m=14.0,
        notes=f"石堡：南北墙高 {H}m、东西墙高 {H_EW}m、墙厚 {T}m；容差 dims ±{DEFAULT_TOL}m，位置 ±{DEFAULT_POS_TOL}m",
        checks=tuple(_checks()),
        precision=precision,
    )
