"""R-C7 / R-C8 冻结期审计：坏检查必须被拦下，好检查不许被误杀。

这一组的关键是**反面**：如果审计对恒真检查、对合法域冲突都无反应，
那它就只是给冻结流程加了一次 0.3 秒的开销，不提供任何保护。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.scene3d import castle_declaration
from app.scene3d.audit import audit_declaration, domain_variants, negative_controls
from app.scene3d.declaration import Declaration, Mount, Precision, Slot
from app.scene3d.gate import CONTRACT_NAME, freeze_contract

CONTRACT = {
    "version": 2,
    "schema_revision": "v2-r2",
    "declaration": "scene.declaration.json",
    "parts": "scene.parts.json",
    "root_scope": "C",
    "bundles": [],
    "scopes": [
        {"id": "C", "kind": "assembly", "children": ["C1"], "importance": 1.0, "connectivity": "required"},
        {"id": "C1", "kind": "bundle", "bundle": "all"},
    ],
}


def _single_bundle_contract(decl: Declaration) -> dict:
    contract = json.loads(json.dumps(CONTRACT))
    contract["bundles"] = [{"id": "all", "slots": [s.name for s in decl.slots], "file": "bundles/all.parts.json"}]
    return contract


def _seed(root: Path, decl: Declaration, contract: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "scene.declaration.json").write_text(decl.dumps(), encoding="utf-8")
    (root / CONTRACT_NAME).write_text(json.dumps(contract), encoding="utf-8")
    return root


def _plate(threshold_min: float, free_h: bool = False) -> Declaration:
    slot = Slot(
        name="plate", builder="wall_panel", mount=Mount((0.0, 0.0, 0.0), 0),
        expect={"w": 4.0, "h": 2.0, "t": 0.2, "openings": []},
        free=("t",) if free_h else (),
        domain={"t": (0.05, 0.40)} if free_h else {},
    )
    check = {
        "id": "plate.present", "rule": "region_filled", "space": "world",
        "requires_slots": ["plate"],
        "box": [0.2, 0.02, 0.2, 3.8, 0.18, 1.8], "min": threshold_min,
    }
    return Declaration(name="plate", slots=(slot,), size_m=4.0, checks=(check,), precision=Precision(0.05, 0.05, 1, 2))


# ---------------------------------------------------------------- 好声明


def test_castle_audit_passes():
    report = audit_declaration(castle_declaration(with_precision=True))
    assert report["ok"], (report["negative_problems"][:2], report["domain_problems"][:2])
    assert report["checks"] == len(castle_declaration().checks)


def test_v1_freeze_does_not_run_audit(tmp_path):
    """v1 是历史契约，不能因为引入审计就把旧项目全卡住。"""
    decl = castle_declaration()
    seed = _seed(tmp_path / "seed", decl, {"version": 1, "declaration": "scene.declaration.json", "parts": "scene.parts.json"})
    assert freeze_contract(tmp_path / "state", "root-v1", str(seed))["version"] == 1


def test_valid_v2_freeze_records_audit_evidence(tmp_path):
    decl = castle_declaration(with_precision=True)
    seed = _seed(tmp_path / "seed", decl, _single_bundle_contract(decl))
    freeze_contract(tmp_path / "state", "root-ok", str(seed))
    snapshot = json.loads((tmp_path / "state" / "scene3d_contracts" / "root-ok" / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["audit"]["ok"] is True
    assert snapshot["audit"]["checks"] == len(decl.checks)


# ---------------------------------------------------------------- 恒真检查（R-C8）


def test_always_true_check_is_rejected():
    """阈值取 0 的 region_filled：无论几何在不在都通过 —— 必须被审计抓出来。"""
    decl = _plate(threshold_min=0.0)
    report = audit_declaration(decl)
    assert not report["ok"]
    issues = {p["issue"] for p in report["negative_problems"]}
    assert "always-true" in issues


def test_strict_check_passes_audit():
    decl = _plate(threshold_min=0.9)
    report = audit_declaration(decl)
    assert report["ok"], report["negative_problems"]
    assert negative_controls(decl) == []


def test_region_empty_with_full_threshold_is_rejected():
    """threshold=1.0 的 region_empty 恒真：任何情况下都"空"。"""
    decl = _plate(threshold_min=0.9)
    raw = decl.to_dict()
    raw["checks"][0].update({"rule": "region_empty", "threshold": 1.0})
    always_true = Declaration.from_dict(raw)
    report = audit_declaration(always_true)
    assert not report["ok"]
    issues = {p["issue"] for p in report["negative_problems"]}
    # 双向审计：阈值 1.0 的 region_empty 既可能是恒真，也可能先在参考实现上就失败
    assert issues & {"always-true", "reference-fails"}


# ---------------------------------------------------------------- 合法域冲突（R-C7）


def test_legal_domain_conflict_is_rejected(tmp_path):
    """放宽窗框 sink 上界后，合法组合会造出埋在墙里的窗框 —— 冻结必须拒绝。"""
    decl = castle_declaration(with_precision=True)
    raw = decl.to_dict()
    widened = False
    for slot in raw["slots"]:
        if slot["slot"] == "wallS.frame0":
            slot["domain"]["sink"] = [0.01, 0.06]
            widened = True
    assert widened
    bad = Declaration.from_dict(raw)
    report = audit_declaration(bad)
    assert not report["ok"]
    assert any("legal-value-fails" in p["issue"] for p in report["domain_problems"])
    seed = _seed(tmp_path / "seed", bad, _single_bundle_contract(bad))
    with pytest.raises(ValueError, match="audit failed"):
        freeze_contract(tmp_path / "state", "root-bad", str(seed))


def test_domain_variants_include_cross_parameter_combinations():
    """单参数角点会漏掉"两个参数同时取极端"的危险组合，必须真的枚举组合。"""
    decl = castle_declaration(with_precision=True)
    variants = [next(iter(mapping.values())) for label, mapping in domain_variants(decl)
                if label.startswith("wallS.frame0:")]
    combos = {(m["sink"], m["bar"]) for m in variants}
    assert (0.03, 0.04) in combos or (0.01, 0.10) in combos
    assert len(combos) >= 8


def test_audit_reports_samples_and_counts():
    report = audit_declaration(_plate(threshold_min=0.9), samples=32)
    assert report["samples"] == 32
    assert report["slots"] == 1
    assert report["checks"] == 1


def test_audit_cli_exit_codes(tmp_path, capsys):
    from app.scene3d.cli import main

    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    good.write_text(_plate(threshold_min=0.9).dumps(), encoding="utf-8")
    bad.write_text(_plate(threshold_min=0.0).dumps(), encoding="utf-8")
    assert main(["audit", "--declaration", str(good)]) == 0
    assert main(["audit", "--declaration", str(bad)]) == 2
    capsys.readouterr()


def test_degeneration_guard_is_not_precision_exit():
    """报告里必须区分退化守卫与精度出口：前者不是 δ 达标证据。"""
    from app.scene3d.checks import evaluate

    decl = castle_declaration(with_precision=True)
    from app.scene3d import reference_parts

    result = evaluate(decl, reference_parts(decl), samples=128)
    ids = {c["id"] for c in result["checks"]}
    assert any(i.startswith("degeneration_guard.") for i in ids)
    assert not any("precision" in i for i in ids)


def test_precision_block_without_exit_is_reported_not_asserted(tmp_path):
    """门禁报告必须显式写 not_evaluated，不能静默省略——省略会被读成"精度已达标"。"""
    from app.scene3d import reference_parts
    from app.scene3d.gate import evaluate_contract

    decl = castle_declaration(with_precision=True)
    seed = _seed(tmp_path / "seed", decl, _single_bundle_contract(decl))
    binding = freeze_contract(tmp_path / "state", "root-p", str(seed))
    delivery = tmp_path / "delivery"
    (delivery / "bundles").mkdir(parents=True)
    (delivery / "scene.declaration.json").write_text(decl.dumps(), encoding="utf-8")
    (delivery / "bundles" / "all.parts.json").write_text(
        json.dumps({"parts": reference_parts(decl)}), encoding="utf-8")
    report = evaluate_contract(tmp_path / "state", "root-p", binding, str(delivery),
                               scope_markers=("scene3d-scope:C1",), depth=1)
    assert report["passed"]
    assert report["precision_exit"] == "not_evaluated"


def test_audit_is_read_only_for_declaration():
    decl = castle_declaration(with_precision=True)
    before = decl.to_dict()
    audit_declaration(decl)
    assert decl.to_dict() == before


def test_replace_keeps_checks_immutable():
    """审计会构造子集声明；原声明的 checks 不能被就地改写。"""
    decl = castle_declaration(with_precision=True)
    original = tuple(dict(c) for c in decl.checks)
    subset = replace(decl, checks=decl.checks[:3])
    assert tuple(dict(c) for c in decl.checks) == original
    assert len(subset.checks) == 3
