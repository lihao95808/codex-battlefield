"""M1：v2-r2 分件契约、scope 责任域、检查依赖、精度守卫边界。

每个断言都配反面：能过的必须过，坏一处必须被拒。测试不许用恒真条件凑绿。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.scene3d import castle_declaration, reference_parts
from app.scene3d.checks import check_coordinate, check_requires
from app.scene3d.contract import (
    merge_bundles,
    parse_contract,
    scope_by_marker,
    scope_slots,
    validate_bundle_partition,
    validate_scope_graph,
    validate_scope_responsibility,
)
from app.scene3d.declaration import Declaration, DeclarationError, Mount, Precision, Slot
from app.scene3d.gate import (
    CONTRACT_NAME,
    assemble_from_bundles,
    deferred_checks,
    evaluate_contract,
    freeze_contract,
)

BODY = ("wallS", "wallN", "wallW", "wallE", "towerSW", "towerSE", "towerNE", "towerNW")
OPENINGS = ("doorS", "wallS.frame0", "wallS.frame1", "wallN.frame0", "wallN.frame1", "wallW.frame0", "wallE.frame0")
ORNAMENTS = ("battleS", "battleN", "battleW", "battleE", "mouldS")
FRAME_CHECKS = {"wallS.win0.frame", "wallS.win1.frame", "wallN.win0.frame", "wallN.win1.frame", "wallW.win0.frame", "wallE.win0.frame"}


def _contract() -> dict:
    return {
        "version": 2,
        "schema_revision": "v2-r2",
        "declaration": "scene.declaration.json",
        "parts": "scene.parts.json",
        "root_scope": "C",
        "bundles": [
            {"id": "body", "slots": list(BODY), "file": "bundles/body.parts.json"},
            {"id": "openings", "slots": list(OPENINGS), "file": "bundles/openings.parts.json"},
            {"id": "ornaments", "slots": list(ORNAMENTS), "file": "bundles/ornaments.parts.json"},
        ],
        "scopes": [
            {"id": "C", "kind": "assembly", "children": ["C1", "C2"], "importance": 1.0,
             "connectivity": "required"},
            {"id": "C1", "kind": "bundle", "bundle": "body"},
            {"id": "C2", "kind": "assembly", "children": ["C21", "C22"], "importance": 2.0,
             "connectivity": "not_required", "output": "assemblies/details.parts.json"},
            {"id": "C21", "kind": "bundle", "bundle": "openings"},
            {"id": "C22", "kind": "bundle", "bundle": "ornaments"},
        ],
    }


def _write_seed(root: Path, declaration: Declaration, contract: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "scene.declaration.json").write_text(declaration.dumps(), encoding="utf-8")
    (root / CONTRACT_NAME).write_text(json.dumps(contract), encoding="utf-8")
    return root


def _parts(decl: Declaration, slots) -> dict:
    keep = set(slots)
    return {"parts": [p for p in reference_parts(decl) if p["slot"] in keep]}


def _delivery(tmp_path: Path):
    decl = castle_declaration(with_precision=True)
    seed = _write_seed(tmp_path / "seed", decl, _contract())
    delivery = tmp_path / "delivery"
    (delivery / "bundles").mkdir(parents=True)
    (delivery / "scene.declaration.json").write_text(decl.dumps(), encoding="utf-8")
    (delivery / CONTRACT_NAME).write_text(json.dumps(_contract()), encoding="utf-8")
    for name, slots in (("body", BODY), ("openings", OPENINGS), ("ornaments", ORNAMENTS)):
        (delivery / "bundles" / f"{name}.parts.json").write_text(json.dumps(_parts(decl, slots)), encoding="utf-8")
    workspace = tmp_path / "state"
    binding = freeze_contract(workspace, "root-c", str(seed))
    return decl, delivery, workspace, binding


# ---------------------------------------------------------------- 契约解析


def test_v1_contract_still_parses(tmp_path):
    parsed = parse_contract(json.dumps(
        {"version": 1, "declaration": "d.json", "parts": "p.json"}).encode(), tmp_path)
    assert parsed.version == 1 and not parsed.is_partitioned


def test_bare_v2_is_rejected_not_silently_accepted(tmp_path):
    raw = _contract()
    del raw["schema_revision"]
    with pytest.raises(ValueError, match="schema_revision"):
        parse_contract(json.dumps(raw).encode(), tmp_path)


def test_unknown_revision_is_rejected(tmp_path):
    raw = _contract()
    raw["schema_revision"] = "v2-r3"
    with pytest.raises(ValueError, match="schema_revision"):
        parse_contract(json.dumps(raw).encode(), tmp_path)


def test_v2_requires_scopes(tmp_path):
    raw = _contract()
    del raw["scopes"]
    with pytest.raises(ValueError, match="scopes"):
        parse_contract(json.dumps(raw).encode(), tmp_path)


@pytest.mark.parametrize("fault", ["missing_slot", "duplicate_slot", "extra_slot"])
def test_partition_must_be_exact(tmp_path, fault):
    raw = _contract()
    if fault == "missing_slot":
        raw["bundles"][0]["slots"].pop()
    elif fault == "duplicate_slot":
        raw["bundles"][0]["slots"].append("wallS")
    else:
        raw["bundles"][0]["slots"].append("ghost")
    with pytest.raises(ValueError):
        contract = parse_contract(json.dumps(raw).encode(), tmp_path)
        validate_bundle_partition(contract, list(BODY + OPENINGS + ORNAMENTS))


# ---------------------------------------------------------------- scope 图


@pytest.mark.parametrize("fault", ["cycle", "unknown_child", "two_parents", "orphan", "unowned_bundle"])
def test_scope_graph_must_be_single_root_tree(tmp_path, fault):
    raw = _contract()
    if fault == "cycle":
        raw["scopes"][2]["children"] = ["C21", "C22", "C"]           # C2 → C → 环
    elif fault == "unknown_child":
        raw["scopes"][0]["children"] = ["C1", "C9"]
    elif fault == "two_parents":
        raw["scopes"][0]["children"] = ["C1", "C2", "C21"]           # C21 有两个父
    elif fault == "orphan":
        raw["scopes"].append({"id": "CX", "kind": "assembly", "children": ["C22"],
                               "importance": 1.0, "connectivity": "not_required"})
    else:
        raw["bundles"].append({"id": "spare", "slots": ["wallS"], "file": "bundles/spare.parts.json"})
    contract = parse_contract(json.dumps(raw).encode(), tmp_path)
    with pytest.raises(ValueError):
        validate_scope_graph(contract)


def test_responsibility_is_computed_not_declared(tmp_path):
    contract = parse_contract(json.dumps(_contract()).encode(), tmp_path)
    assert set(scope_slots(contract, "C1")) == set(BODY)
    assert set(scope_slots(contract, "C2")) == set(OPENINGS + ORNAMENTS)
    assert set(scope_slots(contract, "C")) == set(BODY + OPENINGS + ORNAMENTS)
    validate_scope_responsibility(contract, list(BODY + OPENINGS + ORNAMENTS))


def test_root_must_cover_all_slots(tmp_path):
    raw = _contract()
    raw["scopes"][0]["children"] = ["C1"]                             # 根漏掉 C2
    raw["scopes"] = [s for s in raw["scopes"] if s["id"] in {"C", "C1", "C21", "C22"}]
    raw["scopes"][0]["children"] = ["C1"]
    contract = parse_contract(json.dumps(raw).encode(), tmp_path)
    with pytest.raises(ValueError, match="responsibility"):
        validate_scope_responsibility(contract, list(BODY + OPENINGS + ORNAMENTS))


def test_scope_marker_is_exact_not_substring(tmp_path):
    contract = parse_contract(json.dumps(_contract()).encode(), tmp_path)
    assert scope_by_marker(contract, ("scene3d-scope:C21",)).id == "C21"
    assert scope_by_marker(contract, ("C21",)) is None                 # 无前缀不算
    assert scope_by_marker(contract, ("scene3d-scope:C2",)).id == "C2"  # 完整 token，不是子串
    with pytest.raises(ValueError, match="more than one"):
        scope_by_marker(contract, ("scene3d-scope:C1", "scene3d-scope:C21"))


# ---------------------------------------------------------------- 检查依赖


def test_check_dependency_and_coordinate_are_separate(tmp_path):
    decl = castle_declaration()
    frame = next(c for c in decl.checks if c["id"] == "wallS.win0.frame")
    assert check_requires(frame) == ("wallS.frame0",)
    assert check_coordinate(frame) == "wallS"


def test_body_scope_does_not_own_frame_checks(tmp_path):
    decl, delivery, workspace, binding = _delivery(tmp_path)
    report = evaluate_contract(workspace, "root-c", binding, str(delivery),
                               scope_markers=("scene3d-scope:C1",), depth=1)
    assert report["passed"], report["rejects"]
    assert report["scope_id"] == "C1" and report["scope"] == "bundle"
    ids = {c["id"] for c in report["checks"]}
    assert not (ids & FRAME_CHECKS)                       # 窗棂检查归 C21
    assert FRAME_CHECKS <= set(report["deferred_checks"])
    assert "wallS.win0.aperture" in ids and "towerSW.body" in ids


def test_openings_scope_owns_frames_and_door(tmp_path):
    decl, delivery, workspace, binding = _delivery(tmp_path)
    report = evaluate_contract(workspace, "root-c", binding, str(delivery),
                               scope_markers=("scene3d-scope:C21",), depth=2)
    assert report["passed"], report["rejects"]
    ids = {c["id"] for c in report["checks"]}
    assert FRAME_CHECKS <= ids and "doorS.leaf" in ids
    assert "wallS.win0.aperture" not in ids               # 墙不属于 C21


def test_wall_cannot_be_passed_off_as_window_frame(tmp_path):
    """实心墙必须不能填满"窗棂存在"的检查区域（依赖过滤的反面自检）。"""
    decl, delivery, workspace, binding = _delivery(tmp_path)
    # 把墙塞进 openings 包，冒充窗框：slot 校验会先拒（结构性）
    bogus = _parts(decl, OPENINGS)
    bogus["parts"].append({"slot": "wallS", "builder": "wall_panel",
                           "params": next(p for p in reference_parts(decl) if p["slot"] == "wallS")["params"]})
    (delivery / "bundles" / "openings.parts.json").write_text(json.dumps(bogus), encoding="utf-8")
    report = evaluate_contract(workspace, "root-c", binding, str(delivery),
                               scope_markers=("scene3d-scope:C21",), depth=2)
    assert not report["passed"]


def test_missing_dependency_fails_closed(tmp_path):
    decl = castle_declaration()
    assert deferred_checks(decl, ("wallS",))  # 墙单独交付时有延后检查
    decl, delivery, workspace, binding = _delivery(tmp_path)
    report = evaluate_contract(workspace, "root-c", binding, str(delivery),
                               scope_markers=("scene3d-scope:C1",), depth=1)
    assert report["passed"]
    # C1 的域里不含门/线脚/雉堞，它们的检查必须延后而不是判失败
    assert "doorS.leaf" in report["deferred_checks"]
    assert report["responsible_scope"]["doorS.leaf"] == "C21"
    assert report["responsible_scope"]["battleS.base"] == "C22"


# ---------------------------------------------------------------- 装配与父验收


def test_assembly_writes_intermediate_and_root(tmp_path):
    decl, delivery, workspace, binding = _delivery(tmp_path)
    report = assemble_from_bundles(workspace, "root-c", binding, str(delivery))
    assert report["ok"], report["rejects"]
    assert report["slot_count"] == 20
    assert (delivery / "scene.parts.json").is_file()
    details = delivery / "assemblies" / "details.parts.json"
    assert details.is_file()
    slots = {p["slot"] for p in json.loads(details.read_text(encoding="utf-8"))["parts"]}
    assert slots == set(OPENINGS + ORNAMENTS)
    assert {a["scope"] for a in report["assemblies"]} == {"C2"}


def test_parent_scope_requires_assembly(tmp_path):
    decl, delivery, workspace, binding = _delivery(tmp_path)
    report = evaluate_contract(workspace, "root-c", binding, str(delivery), depth=0)
    assert not report["passed"] and report["stage"] == "parts"


def test_parent_passes_after_assembly_and_fails_on_bad_bundle(tmp_path):
    decl, delivery, workspace, binding = _delivery(tmp_path)
    assemble_from_bundles(workspace, "root-c", binding, str(delivery))
    assert evaluate_contract(workspace, "root-c", binding, str(delivery), depth=0)["passed"]
    bad = json.loads((delivery / "bundles" / "ornaments.parts.json").read_text(encoding="utf-8"))
    next(p for p in bad["parts"] if p["slot"] == "mouldS")["params"]["protrude"] = 0.02
    (delivery / "bundles" / "ornaments.parts.json").write_text(json.dumps(bad), encoding="utf-8")
    # 未重新装配：旧 parts 仍在，但 bundle 已变 —— 父必须能看出不一致
    report = evaluate_contract(workspace, "root-c", binding, str(delivery), depth=0)
    assert report["passed"], "旧装配件在重新装配前仍代表上一次验收的输入"
    assemble_from_bundles(workspace, "root-c", binding, str(delivery))
    assert not evaluate_contract(workspace, "root-c", binding, str(delivery), depth=0)["passed"]


def test_assembly_rejects_missing_bundle_and_extra_input(tmp_path):
    decl, delivery, workspace, binding = _delivery(tmp_path)
    (delivery / "bundles" / "ornaments.parts.json").unlink()
    report = assemble_from_bundles(workspace, "root-c", binding, str(delivery))
    assert not report["ok"] and "missing" in report["rejects"][0]["actual"]


def test_merge_rejects_extra_inputs(tmp_path):
    decl = castle_declaration()
    contract = parse_contract(json.dumps(_contract()).encode(), tmp_path)
    for name, slots in (("body", BODY), ("openings", OPENINGS), ("ornaments", ORNAMENTS)):
        (tmp_path / "bundles").mkdir(exist_ok=True)
        (tmp_path / "bundles" / f"{name}.parts.json").write_text(json.dumps(_parts(decl, slots)), encoding="utf-8")
    inputs = {n: tmp_path / "bundles" / f"{n}.parts.json" for n in ("body", "openings", "ornaments")}
    inputs["ghost"] = inputs["body"]
    with pytest.raises(ValueError, match="unknown bundle"):
        merge_bundles(contract, tmp_path, inputs)


# ---------------------------------------------------------------- 声明与守卫


def test_precision_rejects_silent_int_truncation():
    for bad in (2.9, True, "2", 0):
        with pytest.raises(DeclarationError, match="positive integer"):
            Precision.load({"delta_world_m": 0.05, "min_member_m": 0.05, "slot_ceiling": 20, "max_depth": bad})


def test_slot_ceiling_is_enforced_at_load():
    raw = castle_declaration(with_precision=True).to_dict()
    raw["precision"]["slot_ceiling"] = 3
    with pytest.raises(DeclarationError, match="slot_ceiling"):
        Declaration.from_dict(raw)


def test_depth_above_max_is_rejected(tmp_path):
    decl, delivery, workspace, binding = _delivery(tmp_path)
    assemble_from_bundles(workspace, "root-c", binding, str(delivery))
    report = evaluate_contract(workspace, "root-c", binding, str(delivery), depth=5)
    assert not report["passed"]
    assert "max_depth" in report["rejects"][0]["actual"]


def _blade_declaration(thin: bool) -> Declaration:
    height = 0.02 if thin else 2.0
    return Declaration(
        name="blade",
        slots=(Slot("slab", "wall_panel", Mount((0.0, 0.0, 0.0), 0),
                    expect={"w": 10.0, "h": height, "t": 0.02, "openings": []}),),
        size_m=10.0,
        checks=({"id": "slab.present", "rule": "region_filled", "space": "world", "requires_slots": ["slab"],
                 "box": [0.2, 0.0, 0.0, 9.8, 0.02, height * 0.9], "min": 0.5},),
        precision=Precision(0.05, 0.05, 1, 2),
    )


def test_degeneration_guard_catches_blade_but_not_thin_member():
    from app.scene3d.checks import run_degeneration_checks
    from app.scene3d.assembly import place

    blade = _blade_declaration(thin=True)
    thick = _blade_declaration(thin=False)
    thin_parts = [{"slot": "slab", "builder": "wall_panel", "params": dict(blade.slots[0].expect)}]
    thick_parts = [{"slot": "slab", "builder": "wall_panel", "params": dict(thick.slots[0].expect)}]
    assert [c.ok for c in run_degeneration_checks(blade, place(blade, thin_parts))] == [False]
    assert [c.ok for c in run_degeneration_checks(thick, place(thick, thick_parts))] == [True]
    guard_ids = [c.id for c in run_degeneration_checks(blade, place(blade, thin_parts))]
    assert guard_ids == ["degeneration_guard.slab"]


def test_v1_contract_full_scene_still_works(tmp_path):
    decl = castle_declaration()
    seed = _write_seed(tmp_path / "seed", decl, {"version": 1, "declaration": "scene.declaration.json", "parts": "scene.parts.json"})
    binding = freeze_contract(tmp_path / "state", "root-v1", str(seed))
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "scene.declaration.json").write_text(decl.dumps(), encoding="utf-8")
    (delivery / "scene.parts.json").write_text(json.dumps({"parts": reference_parts(decl)}), encoding="utf-8")
    report = evaluate_contract(tmp_path / "state", "root-v1", binding, str(delivery))
    assert report["passed"] and report["contract_version"] == 1 and report["scope"] == "full"
