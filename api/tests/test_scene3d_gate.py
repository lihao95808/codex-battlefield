"""Observe DONE/events/results, not merely that a gate function was called."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from app import recovery
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, RunKind, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.runner import RunnerEnvelope
from app.scene3d import castle_declaration, reference_parts
from app.scene3d.gate import CONTRACT_NAME, evaluate_contract, freeze_contract
from app.schemas import AcceptanceResult, ChildTaskSpec, VerifierResult, WorkerResult
from app.utils import dumps, loads


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


def seed(path: Path) -> Path:
    path.mkdir()
    declaration = castle_declaration()
    (path / CONTRACT_NAME).write_text(json.dumps({
        "version": 1, "declaration": "scene.declaration.json", "parts": "scene.parts.json",
    }), encoding="utf-8")
    (path / "scene.declaration.json").write_text(declaration.dumps(), encoding="utf-8")
    (path / "scene.parts.json").write_text(json.dumps({"parts": reference_parts(declaration)}), encoding="utf-8")
    return path


def fixture_delivery(tmp_path):
    original = seed(tmp_path / "original")
    delivery = tmp_path / "delivery"
    shutil.copytree(original, delivery)
    workspace = tmp_path / "state"
    binding = freeze_contract(workspace, "root-gate", str(original))
    return original, delivery, workspace, binding


def corrupt(delivery, fault):
    if fault == "geometry":
        path = delivery / "scene.parts.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        # Actual constrained geometry corruption, not a mocked evaluate result.
        data["parts"][0]["params"]["w"] += 2
        path.write_text(json.dumps(data), encoding="utf-8")
    elif fault == "declaration":
        path = delivery / "scene.declaration.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["checks"] = []
        path.write_text(json.dumps(data), encoding="utf-8")
    elif fault == "missing_parts":
        (delivery / "scene.parts.json").unlink()


def envelope(payload, path=None):
    return RunnerEnvelope(command="fake verifier pass", exit_code=0, payload=payload,
                          logs_path=None, prompt_path=None, last_message_path=str(path) if path else None,
                          workdir="fake")


def orchestrator(workspace):
    return Orchestrator(get_settings().__class__(workspace_dir=workspace, mock_delay_seconds=0,
                        constitution_revalidate_enabled=False, agent_burst_limit=0))


def add_root(delivery, binding, status=TaskStatus.VERIFYING):
    with SessionLocal() as db:
        db.add(TaskNode(id="root-gate", title="Complete 3D scene", description="Deliver full scene",
                        status=status.value, worktree_path=str(delivery),
                        node_meta_json=dumps({"scene3d_contract": binding} if binding is not None else {})))
        db.commit()


def observations():
    with SessionLocal() as db:
        root = db.get(TaskNode, "root-gate")
        return root.status, loads(root.result_json, {}), list(db.scalars(
            select(EventRecord).where(EventRecord.task_id == root.id)).all())


def test_geometry_gate_pass_and_frozen_hash(tmp_path):
    original, delivery, workspace, binding = fixture_delivery(tmp_path)
    report = evaluate_contract(workspace, "root-gate", binding, str(delivery))
    assert report["passed"] and report["required"]
    assert len(report["checks"]) == len(castle_declaration().checks) + 1
    assert report["declaration_sha256"] == report["actual_declaration_sha256"]
    assert (workspace / "scene3d_contracts" / "root-gate" / CONTRACT_NAME).read_bytes() == (original / CONTRACT_NAME).read_bytes()
    # Original target is only the seed; later corruption must not affect frozen criteria.
    corrupt(original, "declaration")
    assert evaluate_contract(workspace, "root-gate", binding, str(delivery))["passed"]


@pytest.mark.parametrize("fault,stage", [("geometry", "schema"), ("declaration", "declaration_integrity"),
                                         ("missing_parts", "parts")])
def test_bad_delivery_fails_closed(tmp_path, fault, stage):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    corrupt(delivery, fault)
    report = evaluate_contract(workspace, "root-gate", binding, str(delivery))
    assert not report["passed"] and report["stage"] == stage and report["rejects"]


def test_worker_cannot_opt_out_by_removing_contract(tmp_path):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    (delivery / CONTRACT_NAME).unlink()
    assert evaluate_contract(workspace, "root-gate", binding, str(delivery))["passed"]
    corrupt(delivery, "missing_parts")
    assert not evaluate_contract(workspace, "root-gate", binding, str(delivery))["passed"]


def test_snapshot_tampering_or_loss_never_opts_out(tmp_path):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    snapshot = workspace / "scene3d_contracts" / "root-gate" / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    assert not evaluate_contract(workspace, "root-gate", binding, str(delivery))["passed"]
    snapshot.unlink()
    assert not evaluate_contract(workspace, "root-gate", binding, str(delivery))["passed"]
    assert not evaluate_contract(workspace, "another-root", binding, str(delivery))["passed"]


def test_no_contract_is_unchanged(tmp_path):
    assert freeze_contract(tmp_path, "ordinary", str(tmp_path)) is None
    assert evaluate_contract(tmp_path, "ordinary", None, None)["passed"]
    add_root(tmp_path, None)
    verifier = VerifierResult(verdict="pass", reason="normal pass")
    orchestrator(tmp_path)._record_verifier_result("root-gate", WorkerResult(status="succeeded", summary="ok"),
                                                envelope(verifier.model_dump()), verifier)
    status, result, events = observations()
    assert status == TaskStatus.DONE.value
    assert set(result) == {"worker", "verifier"}
    assert not any(event.type.startswith("scene3d_gate.") for event in events)


@pytest.mark.parametrize("fault", ["geometry", "declaration", "missing_parts"])
def test_fake_verifier_pass_cannot_emit_done_and_keeps_report(tmp_path, fault):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    corrupt(delivery, fault)
    add_root(delivery, binding)
    verifier = VerifierResult(verdict="pass", reason="I claim everything is fine")
    child_ids = orchestrator(workspace)._record_verifier_result(
        "root-gate", WorkerResult(status="succeeded", summary="fake success"), envelope(verifier.model_dump()), verifier)
    status, result, events = observations()
    assert status != TaskStatus.DONE.value and child_ids
    assert result["verifier"]["verdict"] == "repair"
    assert not result["scene3d_gate"]["passed"] and result["scene3d_gate"]["rejects"]
    assert any(event.type == "scene3d_gate.repair_required" for event in events)
    assert not any(event.type == "task.done" for event in events)


def test_real_geometry_pass_permits_done(tmp_path):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    add_root(delivery, binding)
    verifier = VerifierResult(verdict="pass", reason="pass")
    assert orchestrator(workspace)._record_verifier_result(
        "root-gate", WorkerResult(status="succeeded", summary="success"), envelope(verifier.model_dump()), verifier) == []
    status, result, events = observations()
    assert status == TaskStatus.DONE.value and result["scene3d_gate"]["passed"]
    assert any(event.type == "task.done" for event in events)


@pytest.mark.asyncio
async def test_start_freezes_original_target_not_worktree(tmp_path, monkeypatch):
    original = seed(tmp_path / "original")
    orch = orchestrator(tmp_path / "state")
    monkeypatch.setattr(orch, "_open_preview", lambda *_: None)
    constitution_id = await orch.draft_constitution("3D scene", str(original), "mock")
    orch.lock_constitution(constitution_id)
    root_id = await orch.start_project("3D scene", str(original), "mock", constitution_id, auto_run=False)
    with SessionLocal() as db:
        binding = loads(db.get(TaskNode, root_id).node_meta_json, {})["scene3d_contract"]
        assert binding["root_id"] == root_id
    assert evaluate_contract(orch.settings.workspace_dir, root_id, binding, str(original))["passed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_parent", [False, True])
@pytest.mark.parametrize("parent_without_worktree", [False, True])
async def test_parent_acceptance_checks_actual_integration_target_and_keeps_final_report(
    tmp_path, monkeypatch, bad_parent, parent_without_worktree,
):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    child_delivery = tmp_path / "child"
    shutil.copytree(delivery, child_delivery)
    if bad_parent:
        corrupt(delivery, "geometry" if parent_without_worktree else "missing_parts")
    add_root(delivery, binding, TaskStatus.WAITING_CHILDREN)
    with SessionLocal() as db:
        if parent_without_worktree:
            root = db.get(TaskNode, "root-gate")
            root.worktree_path = None
            root.scope_json = dumps([str(delivery)])
        db.add(TaskNode(id="child-done", parent_id="root-gate", title="Full scene child", depth=1,
                        status=TaskStatus.DONE.value, worktree_path=str(child_delivery)))
        db.commit()
    orch = orchestrator(workspace)
    # Integration already happened: keep parent's delivered evidence intact and isolate unrelated gates.
    monkeypatch.setattr(orch, "_integrate_child_outputs", lambda *_: None)
    monkeypatch.setattr(orch, "_pre_acceptance_gate_children", lambda *_: [])
    monkeypatch.setattr(orch, "_parent_godot_integration_gate", lambda *_: {"required": False, "passed": True})
    monkeypatch.setattr(orch, "_final_root_product_gate", lambda *_: {"required": False, "passed": True})
    monkeypatch.setattr(orch, "_record_learning_review", lambda *_: None)
    monkeypatch.setattr(orch, "_write_failure_retrospective", lambda *_: None)
    monkeypatch.setattr(orch, "_consolidate_parent_skill_notes", lambda *_: None)
    monkeypatch.setattr(orch, "_record_acceptance_surface_obligations", lambda *_: None)
    monkeypatch.setattr(orch, "_record_requirement_obligations", lambda *_: None)
    monkeypatch.setattr(orch, "_record_regression_obligations", lambda *_: None)
    monkeypatch.setattr(orch, "_refresh_regression_baseline", lambda *_: None)

    class FakeAcceptance:
        async def accept(self, *args):
            return envelope(AcceptanceResult(verdict="pass", reason="fake pass").model_dump())

    async def stop_after_repair(*args):
        # Don't launch workers or recursively run another fake acceptance.
        with SessionLocal() as db:
            db.get(TaskNode, "root-gate").status = TaskStatus.CANCELED.value
            db.commit()
    monkeypatch.setattr(orch, "_process_children_with_dependencies", stop_after_repair)
    await orch._accept_parent("root-gate", FakeAcceptance(), asyncio.Semaphore(1))
    status, result, events = observations()
    assert result["scene3d_gate"]["passed"] is not bad_parent
    assert result["scene3d_gate"]["target"] == str(delivery)
    assert result["scene3d_gate"]["delivery_source"] == (
        "parent_integration_target" if parent_without_worktree else "worktree")
    assert (status == TaskStatus.DONE.value) is not bad_parent
    assert any(event.type == "task.done" for event in events) is not bad_parent
    if bad_parent:
        assert result["acceptance"]["verdict"] == "repair"
        assert any(event.type == "scene3d_gate.repair_required" for event in events)


def test_verifier_uses_root_binding_not_leaf_metadata_and_preserves_suggestions(tmp_path):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    add_root(delivery, binding, TaskStatus.WAITING_CHILDREN)
    corrupt(delivery, "missing_parts")
    with SessionLocal() as db:
        db.add(TaskNode(id="leaf", parent_id="root-gate", title="Leaf", depth=1,
                        status=TaskStatus.VERIFYING.value, worktree_path=str(delivery), node_meta_json="{}"))
        db.commit()
    verifier = VerifierResult(verdict="pass", reason="fake", suggested_children=[
        ChildTaskSpec(title="Suggested full scene repair", description="Fix complete scene", estimated_leaf=True)])
    child_ids = orchestrator(workspace)._record_verifier_result(
        "leaf", WorkerResult(status="succeeded", summary="fake"), envelope(verifier.model_dump()), verifier)
    with SessionLocal() as db:
        leaf = db.get(TaskNode, "leaf")
        assert leaf.status != TaskStatus.DONE.value
        assert loads(leaf.result_json, {})["scene3d_gate"]["root_id"] == "root-gate"
        assert db.get(TaskNode, child_ids[0]).title == "Suggested full scene repair"


@pytest.mark.parametrize("existing_run", [False, True])
def test_recovered_verifier_pass_runs_same_hard_boundary(tmp_path, monkeypatch, existing_run):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    corrupt(delivery, "missing_parts")
    add_root(delivery, binding)
    orch = orchestrator(workspace)
    worker = WorkerResult(status="succeeded", summary="previous worker succeeded")
    verifier = VerifierResult(verdict="pass", reason="recovered fake pass")
    message = workspace / "verifier-last-message.json"
    raw = dumps(verifier.model_dump()).encode("utf-8")
    message.write_bytes(raw)
    monkeypatch.setattr(orch, "_read_recoverable_last_message", lambda *_: (message, raw, verifier.model_dump()))
    with SessionLocal() as db:
        task = db.get(TaskNode, "root-gate")
        task.result_json = dumps({"worker": worker.model_dump()})
        if existing_run:
            orch._record_run(db, task, RunKind.VERIFIER, envelope(verifier.model_dump(), message), "workspace-write")
            db.flush()
        assert recovery.recover_finished_verifier_message(orch, db, task, 0) == 1
        db.commit()
    status, result, events = observations()
    assert status != TaskStatus.DONE.value and not result["scene3d_gate"]["passed"]
    assert not any(event.type == "task.done" for event in events)
    assert any(event.type == "scene3d_gate.repair_required" for event in events)


@pytest.mark.parametrize("patch", [{"version": True}, {"declaration": "../outside.json"}, {"parts": "scene.declaration.json"}])
def test_invalid_opt_in_rejects_start_instead_of_relaxing(tmp_path, patch):
    original = seed(tmp_path / "original")
    path = original / CONTRACT_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(patch)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        freeze_contract(tmp_path / "state", "invalid", str(original))


def test_leaf_without_worktree_cannot_pass_using_valid_seed_target(tmp_path):
    _, delivery, workspace, binding = fixture_delivery(tmp_path)
    add_root(delivery, binding)
    with SessionLocal() as db:
        root = db.get(TaskNode, "root-gate")
        root.worktree_path = None
        root.scope_json = dumps([str(delivery)])
        db.commit()
    verifier = VerifierResult(verdict="pass", reason="fake leaf pass")
    orchestrator(workspace)._record_verifier_result(
        "root-gate", WorkerResult(status="succeeded", summary="no worktree"),
        envelope(verifier.model_dump()), verifier)
    status, result, events = observations()
    assert status != TaskStatus.DONE.value
    assert not result["scene3d_gate"]["passed"]
    assert result["scene3d_gate"]["target"] is None
    assert result["scene3d_gate"]["delivery_source"] == "missing_worktree"
    assert not any(event.type == "task.done" for event in events)
