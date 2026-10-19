"""scene3d 判据的自检：既要证明它**会通过**，也要证明它**会失败**。

按 AGENTS.md 硬约定 3/5，每个关键断言都配一条反面自检：
- 用参考填写跑全绿（正面）；
- 把某一处改坏，必须有**对应字段**的拒绝（否则断言恒真）；
- 空区域/空场景必须判失败（否则"没采样到"会被读成"没问题"）；
- GLB 只验结构（魔数/长度/accessor 计数），不验"文件存在"。
真正的渲染（Blender）不在单测里跑：45 秒一张，会撞上仓库 120 秒/用例的上限。
"""

from __future__ import annotations

import copy
import json
import struct
from pathlib import Path

import pytest

from app.scene3d import castle_declaration, compare, connectivity, evaluate, place, reference_parts, validate_parts
from app.scene3d.assembly import AssemblyError
from app.scene3d.boxes import Box, box, inside_fraction
from app.scene3d.builders import wall_panel
from app.scene3d.checks import run_checks
from app.scene3d.declaration import Declaration, DeclarationError, Mount
from app.scene3d.glb import write_glb
from app.scene3d.render import RenderError, find_blender


@pytest.fixture(scope="module")
def decl():
    return castle_declaration()


@pytest.fixture(scope="module")
def parts(decl):
    return reference_parts(decl)


def _reject_fields(rejects) -> set[str]:
    return {f"{r.slot}.{r.field}" for r in rejects}


# ------------------------------------------------------------------ 声明


def test_declaration_roundtrip_keeps_every_slot_and_check(decl):
    again = Declaration.loads(decl.dumps())
    assert [s.name for s in again.slots] == [s.name for s in decl.slots]
    assert len(again.checks) == len(decl.checks)
    assert again.size_m == decl.size_m


def test_declaration_rejects_illegal_mount():
    raw = castle_declaration().to_dict()
    raw["slots"][0]["mount"]["rot"] = 45
    with pytest.raises(DeclarationError):
        Declaration.from_dict(raw)


def test_declaration_rejects_free_param_without_domain():
    raw = castle_declaration().to_dict()
    slot = next(s for s in raw["slots"] if s["builder"] == "tower")
    slot["free"] = ["tiers", "tier_h", "top_secret"]
    with pytest.raises(DeclarationError):
        Declaration.from_dict(raw)


def test_declaration_requires_positive_size():
    raw = castle_declaration().to_dict()
    raw["size_m"] = 0
    with pytest.raises(DeclarationError):
        Declaration.from_dict(raw)


# ------------------------------------------------------------------ 桶体分解


def test_wall_panel_decomposition_is_volume_exact():
    w, h, t = 10.0, 7.0, 0.6
    holes = [{"x": 2.1, "z": 2.2, "w": 1.0, "h": 1.6}, {"x": 4.2, "z": 0.0, "w": 1.6, "h": 2.6}]
    boxes = wall_panel({"w": w, "h": h, "t": t, "openings": holes})
    solid = sum(b.volume for b in boxes)
    hole_vol = sum(o["w"] * o["h"] * t for o in holes)
    assert solid == pytest.approx(w * h * t - hole_vol, rel=1e-9)


def test_wall_panel_hole_is_actually_empty():
    boxes = wall_panel({"w": 4.0, "h": 3.0, "t": 0.4, "openings": [{"x": 1.0, "z": 1.0, "w": 1.0, "h": 1.0}]})
    inside_hole, _ = inside_fraction(boxes, box(1.15, 0.05, 1.15, 1.85, 0.35, 1.85), count=128)
    inside_wall, _ = inside_fraction(boxes, box(0.05, 0.05, 0.05, 0.55, 0.35, 0.55), count=128)
    assert inside_hole == 0.0          # 洞里必须是空的
    assert inside_wall == 1.0          # 墙角必须是实的
    # 反面：如果分解把洞漏掉，上面第一条会变成 1.0 → 用例会红


def test_wall_panel_rejects_fully_open_panel():
    with pytest.raises(ValueError):
        wall_panel({"w": 2.0, "h": 2.0, "t": 0.2, "openings": [{"x": 0.0, "z": 0.0, "w": 2.0, "h": 2.0}]})


# ------------------------------------------------------------------ 判定：通过


def test_reference_parts_pass_every_check(decl, parts):
    result = evaluate(decl, parts, samples=256)
    assert result["ok"] is True, result["rejects"]
    # 防止"少跑了几条检查也算通过"
    assert len(result["checks"]) == len(decl.checks) + 1  # mandatory assembly connectivity
    assert result["rejects"] == []


def test_metrics_of_identical_input_is_exactly_one(decl, parts):
    a = place(decl, parts)
    b = place(decl, parts)
    m = compare(a, b, samples=4000)
    assert m["iou"] == 1.0
    assert m["spill"] == 0.0
    assert m["candidate"]["parts"] == len(decl.slots)


# ------------------------------------------------------------------ 判定：必须失败


@pytest.mark.parametrize(
    ("mutate", "expected_field"),
    [
        (lambda ps: ps[0]["params"].__setitem__("w", 9.94), "wallS.params.w"),
        # 小位移 → 逐字段容差拒绝；大位移 → 当成"洞口缺失/错位"（两种都要能被抓到）
        (lambda ps: ps[0]["params"]["openings"][0].__setitem__("z", 2.25), "wallS.params.openings[0].z"),
        (lambda ps: ps[0]["params"]["openings"][0].__setitem__("z", 2.60), "wallS.params.openings[0]"),
        (lambda ps: ps[0]["params"]["openings"].pop(), "wallS.params.openings"),
        (lambda ps: ps[0].__setitem__("builder", "tower"), "wallS.builder"),
        (lambda ps: ps.append({"slot": "ghost", "builder": "tower", "params": {}}), "ghost.slot"),
        (lambda ps: ps.pop(3), None),
        (lambda ps: next(p for p in ps if p["slot"] == "towerSW")["params"].__setitem__("tiers", 9), "towerSW.params.tiers"),
    ],
)
def test_validator_catches_each_kind_of_mistake(decl, parts, mutate, expected_field):
    broken = copy.deepcopy(parts)
    mutate(broken)
    rejects = validate_parts(decl, broken)
    assert rejects, "这次改动没有被任何判据抓到"
    if expected_field is not None:
        assert expected_field in _reject_fields(rejects), _reject_fields(rejects)


def test_bare_wall_fails_aperture_checks(decl, parts):
    """反面自检：墙不挖洞时，窗/门特征必须失败 —— 证明这些判据不是恒真的。"""
    broken = copy.deepcopy(parts)
    wall = next(p for p in broken if p["slot"] == "wallS")
    wall["params"]["openings"] = []
    rejects = validate_parts(decl, broken)
    assert "wallS.params.openings" in _reject_fields(rejects)
    # 绕过 schema（直接构造几何）也要在特征阶段被抓到
    forced = copy.deepcopy(parts)
    for p in forced:
        if p["slot"].startswith("wall"):
            p["params"]["openings"] = []
    assert validate_parts(decl, forced)  # 必须被合规阶段抓到；下面独立证明几何阶段也能抓到
    placed = place(decl, forced)
    results = {r.id: r for r in run_checks(decl, placed, samples=128)}
    assert results["wallS.win0.aperture"].ok is False
    assert results["wallS.door.aperture"].ok is False


def test_empty_region_fails_closed(decl, parts):
    """区域落在场景外 → 采不到样本 → 必须判失败，绝不能因为比例是 0 就通过。"""
    raw = decl.to_dict()
    raw["checks"] = [{
        "id": "ghost.region", "rule": "region_empty", "space": "world",
        "box": [500.0, 500.0, 500.0, 501.0, 501.0, 501.0], "threshold": 0.05,
    }]
    ghost = Declaration.from_dict(raw)
    placed = place(ghost, parts)
    results = run_checks(ghost, placed, samples=64)
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].samples == 0


def test_unknown_rule_fails_closed(decl, parts):
    raw = decl.to_dict()
    c = dict(raw["checks"][0])
    c["rule"] = "looks_fine_to_me"
    raw["checks"] = [c]
    weird = Declaration.from_dict(raw)
    results = run_checks(weird, place(weird, parts), samples=64)
    assert results[0].ok is False


def test_missing_geometry_cannot_be_assembled(decl, parts):
    with pytest.raises(AssemblyError):
        place(decl, [])


def test_detached_tower_breaks_connectivity(decl, parts):
    """把一座塔挪到野外：必须被连通性判出来（V2/V3 的反面自检）。"""
    raw = decl.to_dict()
    for s in raw["slots"]:
        if s["slot"] == "towerSW":
            s["mount"] = {"origin": [200.0, 200.0, 0.0], "rot": 0}
    apart = Declaration.from_dict(raw)
    placed = place(apart, parts)
    conn = connectivity(placed)
    assert conn["single_component"] is False
    assert "towerSW" in conn["isolated"]


def test_metrics_drops_when_parts_are_missing(decl, parts):
    target = place(decl, parts)
    crippled = place(decl, [p for p in parts if p["slot"] != "doorS"])
    m = compare(crippled, target, samples=4000)
    assert m["iou"] < 1.0
    assert m["gap"] > 0.0


# ------------------------------------------------------------------ GLB / 渲染


def test_glb_container_structure(tmp_path: Path):
    path = write_glb(tmp_path / "s.glb", {1: [box(0, 0, 0, 1, 1, 1)], 3: [box(0, 0, 1, 0.2, 0.2, 2)]})
    blob = path.read_bytes()
    magic, version, total = struct.unpack("<III", blob[:12])
    assert magic == 0x46546C67 and version == 2
    assert total == len(blob)  # 头里的长度必须等于真实长度，否则加载器会截断
    jlen, jtype = struct.unpack("<II", blob[12:20])
    assert jtype == 0x4E4F534A
    gltf = json.loads(blob[20:20 + jlen].decode("utf-8"))
    assert len(gltf["meshes"][0]["primitives"]) == 2
    # 每个盒 8 顶点 / 12 三角形
    positions = [a for a in gltf["accessors"] if a["type"] == "VEC3"]
    indices = [a for a in gltf["accessors"] if a["type"] == "SCALAR"]
    assert [a["count"] for a in positions] == [8, 8]
    assert [a["count"] for a in indices] == [36, 36]
    assert gltf["buffers"][0]["byteLength"] <= len(blob)


def test_glb_rejects_empty_scene(tmp_path: Path):
    with pytest.raises(ValueError):
        write_glb(tmp_path / "empty.glb", {1: []})


def test_blender_discovery_fails_closed(tmp_path: Path, monkeypatch):
    with pytest.raises(RenderError):
        find_blender(str(tmp_path / "nope" / "blender.exe"))
    monkeypatch.setenv("CODEX_BATTLEFIELD_BLENDER", str(tmp_path / "also-missing.exe"))
    with pytest.raises(RenderError):
        find_blender(None)


# ------------------------------------------------------------------ CLI


def test_cli_exit_codes(decl, parts, tmp_path: Path, capsys):
    from app.scene3d.cli import main

    dpath = tmp_path / "d.json"
    ppath = tmp_path / "p.json"
    dpath.write_text(decl.dumps(), encoding="utf-8")
    ppath.write_text(json.dumps({"parts": parts}), encoding="utf-8")

    assert main(["validate", "--declaration", str(dpath), "--parts", str(ppath)]) == 0
    bad = copy.deepcopy(parts)
    bad[0]["params"]["w"] = 9.0
    ppath.write_text(json.dumps({"parts": bad}), encoding="utf-8")
    assert main(["validate", "--declaration", str(dpath), "--parts", str(ppath)]) == 2
    assert main(["validate", "--declaration", str(tmp_path / "missing.json"), "--parts", str(ppath)]) == 3
    capsys.readouterr()


def test_cli_reference_matches_helper(decl, tmp_path: Path, capsys):
    from app.scene3d.cli import main

    dpath = tmp_path / "d.json"
    out = tmp_path / "p.json"
    dpath.write_text(decl.dumps(), encoding="utf-8")
    assert main(["reference", "--declaration", str(dpath), "--out", str(out)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))["parts"]
    assert written == reference_parts(decl)
    capsys.readouterr()


def test_mount_transform_rotates_slot_into_expected_place(decl, parts):
    """90° 旋转必须把局部 x 轴映射到世界 y 轴（装配契约的地基）。"""
    placed = place(decl, parts)
    west = next(p for p in placed if p.slot == "wallW")
    # wallW: 局部 x 0..8 → 世界 y 0..8；厚度方向落在 x 0..0.6
    assert west.bbox.x0 == pytest.approx(0.0, abs=1e-6)
    assert west.bbox.x1 == pytest.approx(0.6, abs=1e-6)
    assert west.bbox.y0 == pytest.approx(0.0, abs=1e-6)
    assert west.bbox.y1 == pytest.approx(8.0, abs=1e-6)


def test_mount_rot_180_flips_both_axes(decl):
    raw = castle_declaration().to_dict()
    slot = next(s for s in raw["slots"] if s["slot"] == "wallN")
    assert slot["mount"]["rot"] == 180
    m = Mount.load(slot["mount"], "wallN")
    assert m.origin == (10.0, 8.0, 0.0)


def test_box_helpers_are_consistent_with_sampling():
    b = Box(0.0, 0.0, 0.0, 2.0, 2.0, 2.0)
    frac, n = inside_fraction((b,), box(0.5, 0.5, 0.5, 1.5, 1.5, 1.5), count=64)
    assert n == 64
    assert frac == 1.0
    frac, _ = inside_fraction((b,), box(3.0, 3.0, 3.0, 4.0, 4.0, 4.0), count=64)
    assert frac == 0.0
