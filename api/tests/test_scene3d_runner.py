"""Executable runner-tool wiring and fail-closed regressions; no LLM calls."""
from __future__ import annotations

import copy
import importlib
import json
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import prompts, runner_tools
from app.config import Settings
from app.models import TaskNode
from app.scene3d import castle_declaration, evaluate, place, reference_parts
from app.scene3d.assembly import PlacedPart, connectivity
from app.scene3d.boxes import box
from app.scene3d.declaration import Declaration, DeclarationError, Mount
from app.scene3d.render import RenderError


def test_deployed_tool_executes_outside_application_checkout(tmp_path):
    tool = runner_tools.ensure_scene3d_tool(tmp_path / "workspace")
    source = Path(runner_tools.__file__).parent / "scene3d"
    copied = tool.parent / "scene3d"
    assert {p.name for p in source.glob("*.py")} == {p.name for p in copied.glob("*.py")}
    for p in source.glob("*.py"):
        assert p.read_bytes().replace(b"\r\n", b"\n") == (copied / p.name).read_bytes().replace(b"\r\n", b"\n")
    declaration = tmp_path / "declaration.json"
    submission = tmp_path / "parts.json"
    decl = castle_declaration()
    declaration.write_text(decl.dumps(), encoding="utf-8")
    parts = reference_parts(decl)
    submission.write_text(json.dumps({"parts": parts}), encoding="utf-8")
    args = [sys.executable, str(tool), "validate", "--declaration", str(declaration), "--parts", str(submission), "--json"]
    good = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert good.returncode == 0, good.stderr
    report = json.loads(good.stdout)
    assert report["ok"] and any(c["id"] == "assembly.connectivity" for c in report["checks"])
    parts[0]["params"]["w"] = 1.0
    submission.write_text(json.dumps({"parts": parts}), encoding="utf-8")
    bad = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert bad.returncode == 2
    assert json.loads(bad.stdout)["stage"] == "schema"


def test_real_role_prompts_offer_scene3d_only_for_3d_tasks(tmp_path):
    settings = Settings(workspace_dir=tmp_path)
    task = TaskNode(id="scene-tool-test", title="Blender 3D castle", description="build box assembly")
    worker = prompts.worker_prompt(settings, task)
    assert "SCENE3D_TOOL" in worker and "scene3d_tool.py" in worker
    assert "render --declaration" in worker
    from app.schemas import WorkerResult
    verifier = prompts.verifier_prompt(settings, task, WorkerResult(status="succeeded", summary="scene built"))
    assert "SCENE3D_TOOL" in verifier
    assert "SCENE3D_TOOL" in prompts.acceptance_prompt(settings, task, [])
    assert "Run validate independently" in verifier
    assert "render --declaration" not in verifier
    task.title = "Fix CSV export"
    assert "SCENE3D_TOOL" not in prompts.worker_prompt(settings, task)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "10"])
def test_worker_nonnumeric_or_nonfinite_cannot_pass(value):
    decl = castle_declaration()
    parts = reference_parts(decl)
    parts[0]["params"]["w"] = value
    assert not evaluate(decl, parts)["ok"]


def test_empty_checks_and_degenerate_regions_fail_closed():
    decl = castle_declaration()
    parts = reference_parts(decl)
    assert not evaluate(replace(decl, checks=()), parts)["ok"]
    raw = decl.to_dict()
    raw["checks"] = []
    with pytest.raises(DeclarationError):
        Declaration.from_dict(raw)
    check = dict(decl.checks[0], box=[0, 0, 0, 0, 1, 1])
    assert not evaluate(replace(decl, checks=(check,)), parts)["ok"]


def test_reference_answer_does_not_mutate_locked_declaration():
    decl = castle_declaration()
    original = copy.deepcopy(decl.slots[0].expect)
    parts = reference_parts(decl)
    parts[0]["params"]["openings"][0]["w"] = 123
    assert decl.slots[0].expect == original


def test_connectivity_is_mandatory_and_does_not_bridge_empty_aabbs():
    decl = castle_declaration()
    slots = tuple(replace(s, mount=Mount((100, 100, 0))) if s.name == "towerSW" else s for s in decl.slots)
    result = evaluate(replace(decl, slots=slots), reference_parts(decl))
    assert not result["ok"]
    assert any(c["id"] == "assembly.connectivity" and not c["ok"] for c in result["checks"])
    a_boxes = (box(0, 0, 0, 1, 1, 1), box(4, 0, 0, 5, 1, 1))
    a = PlacedPart("a", "tower", a_boxes, a_boxes, box(0, 0, 0, 5, 1, 1))
    b_boxes = (box(2, 0, 0, 3, 1, 1),)
    b = PlacedPart("b", "tower", b_boxes, b_boxes, b_boxes[0])
    assert not connectivity([a, b])["single_component"]
    assert not connectivity([])["single_component"]


@pytest.mark.parametrize("mode", ["no-output", "nonzero", "fake-image"])
def test_render_cannot_accept_old_or_fake_artifacts(tmp_path, monkeypatch, mode):
    module = importlib.import_module("app.scene3d.render")
    old = tmp_path / "scene3d_hero.png"
    old.write_bytes(b"old output" * 1000)
    monkeypatch.setattr(module, "find_blender", lambda _: Path(sys.executable))
    def fake_run(args, **kwargs):
        cfg = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
        target = Path(cfg["out_dir"]) / "scene3d_hero.png"
        if mode == "fake-image":
            target.write_bytes(b"not a PNG" * 1000)
        elif mode == "nonzero":
            target.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1280, 880) + b"x" * 6000)
        return SimpleNamespace(returncode=1 if mode == "nonzero" else 0)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    decl = castle_declaration()
    with pytest.raises(RenderError):
        module.render(decl, place(decl, reference_parts(decl)), tmp_path, views=["hero"])
    assert old.read_bytes().startswith(b"old output")


def test_describe_is_compact_contract_not_reference_answer(tmp_path, capsys):
    from app.scene3d.cli import main
    decl = castle_declaration()
    path = tmp_path / "scene.declaration.json"
    path.write_text(decl.dumps(), encoding="utf-8")
    assert main(["describe", "--declaration", str(path)]) == 0
    text = capsys.readouterr().out
    data = json.loads(text)
    assert data["check_count"] == len(decl.checks)
    assert len(data["slots"]) == len(decl.slots)
    assert "checks" not in data
    assert len(text) < len(decl.dumps())
    for slot, source in zip(data["slots"], decl.slots, strict=True):
        assert slot["expect"] == source.expect
        assert "params" not in slot
        assert not set(slot["free"]) & set(slot["expect"])


def test_glb_faces_point_outward():
    from app.scene3d.glb import _boxes_to_arrays
    positions, indices = _boxes_to_arrays([box(0, 0, 0, 1, 1, 1)])
    verts = [positions[i:i + 3] for i in range(0, len(positions), 3)]
    for i in range(0, len(indices), 3):
        a, b, c = [verts[j] for j in indices[i:i + 3]]
        u = [b[k] - a[k] for k in range(3)]
        v = [c[k] - a[k] for k in range(3)]
        normal = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        outward = [(a[k] + b[k] + c[k]) / 3 - 0.5 for k in range(3)]
        assert sum(normal[k] * outward[k] for k in range(3)) > 0

