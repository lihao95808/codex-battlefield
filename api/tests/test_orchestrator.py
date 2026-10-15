from __future__ import annotations

import os
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest

from app.config import get_settings
from app.database import _should_block_destructive_sql, engine, init_db, SessionLocal
from app.models import Base, EventRecord, RunKind, RunRecord, RunStatus, TaskNode, TaskStatus
import app.orchestrator as orchestrator_module
from app.orchestrator import (
    CODEX_STARTUP_NO_OUTPUT_GRACE_SECONDS,
    Orchestrator,
    PENDING_CODEX_SLOT_RESERVATION_SECONDS,
)
from app.runner import BaseCodexRunner, CodexExecRunner, RunnerEnvelope, is_meaningful_codex_activity_text
from app.schemas import AcceptanceResult, ChildTaskSpec, PlannerDecision, VerifierResult, WorkerResult
from app.utils import dumps, loads


ARCADE_BRAWLER_GOAL = "开发一个2D 街机 格斗 闯关 游戏 godot 品质和市面上街机一个水平"


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


async def locked_constitution(orchestrator: Orchestrator, goal: str) -> str:
    constitution_id = await orchestrator.draft_constitution(goal, runner_mode="mock")
    orchestrator.lock_constitution(constitution_id)
    return constitution_id


async def drain_background_tasks(orchestrator: Orchestrator, timeout_seconds: float = 10.0) -> None:
    for _ in range(10):
        tasks = list(orchestrator._background_tasks)
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout_seconds)


@pytest.mark.asyncio
async def test_kick_tree_schedules_existing_waiting_parent_without_reset(monkeypatch):
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    root = TaskNode(id="kick-root", title="Root", description="Keep existing tree.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
    child = TaskNode(id="kick-child", parent_id=root.id, title="Child", description="", depth=1, status=TaskStatus.QUEUED.value)
    with SessionLocal() as db:
        db.add_all([root, child])
        db.commit()

    calls: list[tuple[str, str | None]] = []

    async def fake_run_tree(root_id: str, runner_mode: str | None = None) -> None:
        calls.append((root_id, runner_mode))

    monkeypatch.setattr(orchestrator, "run_tree", fake_run_tree)

    result = await orchestrator.kick_tree(root.id, "real")
    await drain_background_tasks(orchestrator)

    assert result == {"status": "queued", "root_id": root.id}
    assert calls == [(root.id, "real")]
    with SessionLocal() as db:
        stored_root = db.get(TaskNode, root.id)
        assert stored_root is not None
        assert stored_root.status == TaskStatus.WAITING_CHILDREN.value
        assert db.get(TaskNode, child.id) is not None
        event = db.query(EventRecord).filter(EventRecord.task_id == root.id, EventRecord.type == "scheduler.kick").one()
        assert "继续推进现有子树" in event.message


@pytest.mark.asyncio
async def test_goal_mode_real_kick_uses_detached_scheduler(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        goal_mode=True,
        runner_mode="real",
    )
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="detached-kick-root",
        title="Root",
        description="Keep API as control plane.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
        node_meta_json=dumps({"runner_mode": "real"}),
    )
    child = TaskNode(id="detached-kick-child", parent_id=root.id, title="Child", description="", depth=1, status=TaskStatus.QUEUED.value)
    with SessionLocal() as db:
        db.add_all([root, child])
        db.commit()

    launched: list[tuple[str, str | None]] = []

    def fake_spawn(root_id: str, runner_mode: str | None = None) -> dict[str, object]:
        launched.append((root_id, runner_mode))
        return {"status": "queued", "root_id": root_id, "scheduler": "detached", "pid": 123}

    monkeypatch.setattr(orchestrator, "_spawn_detached_tree_runner", fake_spawn)

    result = await orchestrator.kick_tree(root.id, "real")

    assert result == {"status": "queued", "root_id": root.id, "scheduler": "detached", "pid": 123}
    assert launched == [(root.id, "real")]
    assert not orchestrator._background_tasks


@pytest.mark.asyncio
async def test_hibernate_tree_persists_state_and_manual_resume():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    constitution_id = await locked_constitution(orchestrator, "Build a hibernation probe.")
    root_id = await orchestrator.start_project(
        goal="Build a hibernation probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )

    hibernated = await orchestrator.hibernate_tree(root_id, 60, "等待 token 池刷新")

    assert hibernated["status"] == "hibernating"
    assert hibernated["count"] == 1
    with SessionLocal() as db:
        task = db.get(TaskNode, root_id)
        assert task is not None
        assert task.status == TaskStatus.HIBERNATING.value
        meta = loads(task.node_meta_json, {})
        assert meta["hibernation"]["owner_id"] == root_id
        assert meta["hibernation"]["reason"] == "等待 token 池刷新"

    resumed = await orchestrator.resume_hibernated_tree(root_id)

    assert resumed["count"] == 1
    assert root_id not in orchestrator._auto_resume_tasks
    await orchestrator.cancel_all(root_id)


def test_get_tree_compacts_done_subtrees_into_parent_archive():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    root = TaskNode(id="archive-root", title="Root", description="Root.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
    done_child = TaskNode(id="archive-done-child", parent_id=root.id, title="Done child", description="", depth=1, status=TaskStatus.DONE.value)
    done_leaf = TaskNode(id="archive-done-leaf", parent_id=done_child.id, title="Done leaf", description="", depth=2, status=TaskStatus.DONE.value)
    active_child = TaskNode(id="archive-active-child", parent_id=root.id, title="Active child", description="", depth=1, status=TaskStatus.QUEUED.value)
    with SessionLocal() as db:
        db.add_all([root, done_child, done_leaf, active_child])
        db.commit()

        compact_tree = orchestrator.get_tree(db, root_id=root.id, compact_done=True)
        compact_ids = {task["id"] for task in compact_tree["tasks"]}
        root_payload = next(task for task in compact_tree["tasks"] if task["id"] == root.id)

        expanded_tree = orchestrator.get_tree(db, root_id=root.id, compact_done=True, expanded_done_parent_ids=[root.id])
        expanded_ids = {task["id"] for task in expanded_tree["tasks"]}
        expanded_root = next(task for task in expanded_tree["tasks"] if task["id"] == root.id)

    assert compact_ids == {root.id, active_child.id}
    assert compact_tree["edges"] == [{"source": root.id, "target": active_child.id}]
    assert compact_tree["stats"]["total"] == 4
    assert compact_tree["stats"]["visible_total"] == 2
    assert compact_tree["stats"]["archived_done_nodes"] == 2
    assert root_payload["node_meta"]["done_archive"]["node_count"] == 2
    assert root_payload["node_meta"]["done_archive"]["root_count"] == 1
    assert root_payload["node_meta"]["done_archive"]["compacted"] is True

    assert expanded_ids == {root.id, done_child.id, done_leaf.id, active_child.id}
    assert expanded_tree["stats"]["visible_total"] == 4
    assert expanded_tree["stats"]["archived_done_nodes"] == 0
    assert expanded_root["node_meta"]["done_archive"]["expanded"] is True


def test_resource_pressure_ignores_local_codex_skill_metadata_failures():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    task = TaskNode(
        id="local-skill-block",
        title="Local skill failure",
        description="Codex local skill metadata failed before model work.",
        depth=0,
        status=TaskStatus.BLOCKED.value,
        node_meta_json=dumps(
            {
                "block_report": {
                    "category": "rate_limit",
                    "retryable": True,
                    "source": "verifier",
                    "reason": (
                        "Codex verifier exited with code 1. "
                        "ERROR codex_core::session::session: failed to load skill "
                        "C:/Users/example/.codex/plugins/cache/heygen-avatar/SKILL.md: "
                        "invalid description: exceeds maximum length of 1024 characters"
                    ),
                }
            }
        ),
    )
    with SessionLocal() as db:
        db.add(task)
        db.commit()
        pressure, reason = orchestrator._resource_pressure_state(db, [task])

    assert pressure is False
    assert reason == ""


@pytest.mark.asyncio
async def test_hibernate_tree_normalizes_unreadable_reason_to_budget_context():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        agent_burst_limit=8,
        agent_burst_reserve=1,
        machine_safety_min_available_memory_mb=0,
    )
    orchestrator = Orchestrator(settings)
    constitution_id = await locked_constitution(orchestrator, "Build a readable hibernation reason probe.")
    root_id = await orchestrator.start_project(
        goal="Build a readable hibernation reason probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )
    with SessionLocal() as db:
        orchestrator._set_agent_burst_used_for_root(db, root_id, 7)
        db.commit()

    await orchestrator.hibernate_tree(root_id, 60, "????????????????? Codex/token ?????????")

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        reason = meta["hibernation"]["reason"]

    assert "达到本轮 Codex agent 工作启动额度 7/7" in reason
    assert "额外保留 1 次预算" in reason
    assert "????" not in reason

    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)


@pytest.mark.asyncio
async def test_watchdog_state_falls_back_for_legacy_unreadable_hibernation_reason():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        agent_burst_limit=8,
        agent_burst_reserve=1,
        machine_safety_min_available_memory_mb=0,
    )
    orchestrator = Orchestrator(settings)
    constitution_id = await locked_constitution(orchestrator, "Build a legacy watchdog reason probe.")
    root_id = await orchestrator.start_project(
        goal="Build a legacy watchdog reason probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        orchestrator._set_agent_burst_used_for_root(db, root_id, 7)
        meta = loads(root.node_meta_json, {})
        meta["hibernation"] = {
            "owner_id": root_id,
            "previous_status": TaskStatus.QUEUED.value,
            "resume_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "reason": "????????????????? Codex/token ?????????",
            "status": "scheduled",
            "scheduled_at": datetime.now(timezone.utc).isoformat(),
        }
        root.node_meta_json = dumps(meta)
        root.status = TaskStatus.HIBERNATING.value
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root_id)
        repaired_meta = loads(root.node_meta_json, {})

    assert "达到本轮 Codex agent 工作启动额度 7/7" in state["reason"]
    assert "额外保留 1 次预算" in state["reason"]
    assert "????" not in state["reason"]
    assert repaired_meta["hibernation"]["reason"] == state["reason"]
    assert repaired_meta["hibernation"]["reason_repaired_at"]


def test_watchdog_state_reports_active_agent_progress_diagnostics(tmp_path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        codex_idle_timeout_seconds=100,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-agent-diagnostics"
    child_id = "node-agent-diagnostics"
    run_dir = tmp_path / "runs" / child_id
    run_dir.mkdir(parents=True)
    log_path = run_dir / "worker.jsonl"
    old_timestamp = time.time() - 160
    log_timestamp = datetime.fromtimestamp(old_timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_path.write_text(
        f"{log_timestamp} WARN stream disconnected\n"
        '{"type":"error","message":"Reconnecting... 5/5 (request timed out)"}\n'
        f"{log_timestamp} WARN falling back to HTTP\n",
        encoding="utf-8",
    )
    os.utime(log_path, (old_timestamp, old_timestamp))
    prompt_path = run_dir / "worker-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    os.utime(prompt_path, (old_timestamp - 10, old_timestamp - 10))

    with SessionLocal() as db:
        root = TaskNode(
            id=root_id,
            title="Root",
            description="Root task",
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Worker that stopped writing",
            description="Worker task",
            depth=1,
            status=TaskStatus.WORKING.value,
        )
        db.add_all([root, child])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    diagnostics = state["active_agent_diagnostics"]
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic["task_id"] == child_id
    assert diagnostic["phase"] == RunKind.WORKER.value
    assert diagnostic["severity"] == "stalled"
    assert diagnostic["no_progress_seconds"] >= 100
    assert "reconnecting" in diagnostic["markers"]
    assert "http_fallback" in diagnostic["markers"]


def test_cleanup_stalled_external_codex_agents_requeues_and_stops(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=100,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-stalled-cleanup"
    child_id = "node-stalled-cleanup"
    run_dir = settings.workspace_dir / "runs" / child_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "worker-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    old_timestamp = time.time() - 180
    os.utime(prompt_path, (old_timestamp, old_timestamp))
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": 1234, "task_id": child_id, "status": TaskStatus.WORKING.value}],
    )
    stopped_pids: list[int] = []

    def fake_stop(pid: int) -> dict[str, object]:
        stopped_pids.append(pid)
        return {"pid": pid, "stopped": True, "exit_code": 0, "stdout": "SUCCESS"}

    monkeypatch.setattr(orchestrator, "_stop_external_codex_process_tree", fake_stop)

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Stalled worker",
            description="Worker task",
            depth=1,
            status=TaskStatus.WORKING.value,
        )
        db.add_all([root, child])
        db.commit()

    result = orchestrator.cleanup_stalled_external_codex_agents(root_id)

    assert result["status"] == "cleaned"
    assert result["stalled"] == 1
    assert result["requeued"] == 1
    assert stopped_pids == [1234]
    assert child_id in orchestrator._pending_codex_slot_task_ids()
    with SessionLocal() as db:
        task = db.get(TaskNode, child_id)
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        meta = loads(task.node_meta_json, {})
        assert meta["external_stall_cleanup"]["phase"] == RunKind.WORKER.value
        event = (
            db.query(EventRecord)
            .filter(EventRecord.task_id == child_id, EventRecord.type == "watchdog.external_stall_requeued")
            .one()
        )
        assert loads(event.payload_json, {})["pending_slot_grace_seconds"] > 0


def test_cleanup_stalled_external_codex_agents_requeues_active_state_without_process(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=100,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-stalled-no-process"
    child_id = "node-stalled-no-process"
    run_dir = settings.workspace_dir / "runs" / child_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "verifier-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    old_timestamp = time.time() - 180
    os.utime(prompt_path, (old_timestamp, old_timestamp))
    monkeypatch.setattr(orchestrator, "_external_codex_processes", lambda max_cache_age_seconds=2.0: [])

    def fail_stop(pid: int) -> dict[str, object]:
        raise AssertionError("no process should be stopped")

    monkeypatch.setattr(orchestrator, "_stop_external_codex_process_tree", fail_stop)

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Verifier state without process",
            description="Verifier task",
            depth=1,
            status=TaskStatus.VERIFYING.value,
        )
        db.add_all([root, child])
        db.commit()

    result = orchestrator.cleanup_stalled_external_codex_agents(root_id)

    assert result["status"] == "cleaned"
    assert result["requeued"] == 1
    assert result["stopped"][0]["reason"] == "stalled_active_status_without_live_process"
    with SessionLocal() as db:
        task = db.get(TaskNode, child_id)
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value


def test_cleanup_stalled_external_codex_agents_requeues_empty_last_message_without_process(
    monkeypatch, tmp_path: Path
):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=100,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-empty-last-message"
    child_id = "node-empty-last-message"
    run_dir = settings.workspace_dir / "runs" / child_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "verifier-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    last_message_path = run_dir / "verifier-last-message.json"
    last_message_path.write_bytes(b"")
    old_timestamp = time.time() - 180
    os.utime(prompt_path, (old_timestamp, old_timestamp))
    os.utime(last_message_path, (old_timestamp, old_timestamp))
    monkeypatch.setattr(orchestrator, "_external_codex_processes", lambda max_cache_age_seconds=2.0: [])

    def fail_stop(pid: int) -> dict[str, object]:
        raise AssertionError("no process should be stopped")

    monkeypatch.setattr(orchestrator, "_stop_external_codex_process_tree", fail_stop)

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Verifier state with empty last-message",
            description="Verifier task",
            depth=1,
            status=TaskStatus.VERIFYING.value,
        )
        db.add_all([root, child])
        db.commit()

    result = orchestrator.cleanup_stalled_external_codex_agents(root_id)

    assert result["status"] == "cleaned"
    assert result["requeued"] == 1
    assert result["skipped"] == []
    assert result["stopped"][0]["reason"] == "stalled_active_status_without_live_process"
    with SessionLocal() as db:
        task = db.get(TaskNode, child_id)
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        meta = loads(task.node_meta_json, {})
        assert meta["external_stall_cleanup"]["reason"] == "stalled_active_status_without_live_process"


def test_cleanup_stalled_external_codex_agents_requeues_failed_active_state_without_process(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-failed-active-no-process"
    child_id = "node-failed-active-no-process"
    run_dir = settings.workspace_dir / "runs" / child_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "acceptance-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    now = time.time()
    os.utime(prompt_path, (now, now))
    monkeypatch.setattr(orchestrator, "_external_codex_processes", lambda max_cache_age_seconds=2.0: [])

    def fail_stop(pid: int) -> dict[str, object]:
        raise AssertionError("no process should be stopped")

    monkeypatch.setattr(orchestrator, "_stop_external_codex_process_tree", fail_stop)

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Failed acceptance without process",
            description="Acceptance task",
            depth=1,
            status=TaskStatus.ACCEPTING.value,
        )
        run = RunRecord(
            task_id=child_id,
            kind=RunKind.ACCEPTANCE.value,
            status=RunStatus.FAILED.value,
            exit_code=1,
            prompt_path=str(prompt_path),
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        db.add_all([root, child, run])
        db.commit()

    result = orchestrator.cleanup_stalled_external_codex_agents(root_id)

    assert result["status"] == "cleaned"
    assert result["stalled"] == 0
    assert result["requeued"] == 1
    assert result["stopped"][0]["reason"] == "failed_run_without_live_process"
    assert result["skipped"] == []
    with SessionLocal() as db:
        task = db.get(TaskNode, child_id)
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        meta = loads(task.node_meta_json, {})
        assert meta["external_stall_cleanup"]["reason"] == "failed_run_without_live_process"
        event = (
            db.query(EventRecord)
            .filter(EventRecord.task_id == child_id, EventRecord.type == "watchdog.external_stall_requeued")
            .one()
        )
        payload = loads(event.payload_json, {})
        assert payload["cleanup_reason"] == "failed_run_without_live_process"
        assert payload["pending_slot_grace_seconds"] > 0


def test_cleanup_stalled_external_codex_agents_requeues_active_state_without_process_after_grace(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-active-grace-no-process"
    child_id = "node-active-grace-no-process"
    run_dir = settings.workspace_dir / "runs" / child_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "acceptance-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    old_timestamp = time.time() - PENDING_CODEX_SLOT_RESERVATION_SECONDS - 10
    os.utime(prompt_path, (old_timestamp, old_timestamp))
    monkeypatch.setattr(orchestrator, "_external_codex_processes", lambda max_cache_age_seconds=2.0: [])

    def fail_stop(pid: int) -> dict[str, object]:
        raise AssertionError("no process should be stopped")

    monkeypatch.setattr(orchestrator, "_stop_external_codex_process_tree", fail_stop)

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Acceptance state without process",
            description="Acceptance task",
            depth=1,
            status=TaskStatus.ACCEPTING.value,
        )
        run = RunRecord(
            task_id=child_id,
            kind=RunKind.ACCEPTANCE.value,
            status=RunStatus.SUCCEEDED.value,
            exit_code=0,
            started_at=datetime.now(timezone.utc) - timedelta(days=1),
            finished_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.add_all([root, child, run])
        db.commit()

    result = orchestrator.cleanup_stalled_external_codex_agents(root_id)

    assert result["status"] == "cleaned"
    assert result["stalled"] == 0
    assert result["requeued"] == 1
    assert result["stopped"][0]["reason"] == "active_status_without_live_process_after_startup_grace"
    assert result["skipped"] == []
    with SessionLocal() as db:
        task = db.get(TaskNode, child_id)
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        meta = loads(task.node_meta_json, {})
        assert meta["external_stall_cleanup"]["reason"] == "active_status_without_live_process_after_startup_grace"


def test_cleanup_stalled_external_codex_agents_keeps_recent_active_state_without_process(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-recent-active-no-process"
    child_id = "node-recent-active-no-process"
    run_dir = settings.workspace_dir / "runs" / child_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "acceptance-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    now = time.time()
    os.utime(prompt_path, (now, now))
    monkeypatch.setattr(orchestrator, "_external_codex_processes", lambda max_cache_age_seconds=2.0: [])

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Recent acceptance state",
            description="Acceptance task",
            depth=1,
            status=TaskStatus.ACCEPTING.value,
        )
        db.add_all([root, child])
        db.commit()

    result = orchestrator.cleanup_stalled_external_codex_agents(root_id)

    assert result["status"] == "ok"
    assert result["requeued"] == 0
    with SessionLocal() as db:
        task = db.get(TaskNode, child_id)
        assert task is not None
        assert task.status == TaskStatus.ACCEPTING.value


def test_cleanup_stalled_external_codex_agents_requeues_live_process_without_current_output(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-live-no-output"
    child_id = "node-live-no-output"
    run_dir = settings.workspace_dir / "runs" / child_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "acceptance-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    old_timestamp = time.time() - CODEX_STARTUP_NO_OUTPUT_GRACE_SECONDS - 10
    os.utime(prompt_path, (old_timestamp, old_timestamp))
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [
            {"pid": 1234, "task_id": child_id, "status": TaskStatus.ACCEPTING.value}
        ],
    )
    stopped_pids: list[int] = []

    def stop_process(pid: int) -> dict[str, object]:
        stopped_pids.append(pid)
        return {"pid": pid, "stopped": True, "exit_code": 0, "stdout": "stopped"}

    monkeypatch.setattr(orchestrator, "_stop_external_codex_process_tree", stop_process)

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Live process without output",
            description="Acceptance task",
            depth=1,
            status=TaskStatus.ACCEPTING.value,
        )
        db.add_all([root, child])
        db.commit()

    result = orchestrator.cleanup_stalled_external_codex_agents(root_id)

    assert result["status"] == "cleaned"
    assert result["requeued"] == 1
    assert stopped_pids == [1234]
    assert result["stopped"][0]["reason"] == "live_process_without_current_output_after_startup_grace"
    with SessionLocal() as db:
        task = db.get(TaskNode, child_id)
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        meta = loads(task.node_meta_json, {})
        assert meta["external_stall_cleanup"]["reason"] == "live_process_without_current_output_after_startup_grace"
        event = (
            db.query(EventRecord)
            .filter(EventRecord.task_id == child_id, EventRecord.type == "watchdog.external_stall_requeued")
            .one()
        )
        payload = loads(event.payload_json, {})
        assert payload["cleanup_reason"] == "live_process_without_current_output_after_startup_grace"
        assert payload["startup_no_output_grace_seconds"] == CODEX_STARTUP_NO_OUTPUT_GRACE_SECONDS


def test_cleanup_stalled_external_codex_agents_keeps_recent_live_process_without_current_output(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-recent-live-no-output"
    child_id = "node-recent-live-no-output"
    run_dir = settings.workspace_dir / "runs" / child_id
    run_dir.mkdir(parents=True)
    prompt_path = run_dir / "acceptance-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    recent_timestamp = time.time() - CODEX_STARTUP_NO_OUTPUT_GRACE_SECONDS + 10
    os.utime(prompt_path, (recent_timestamp, recent_timestamp))
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [
            {"pid": 1234, "task_id": child_id, "status": TaskStatus.ACCEPTING.value}
        ],
    )

    def fail_stop(pid: int) -> dict[str, object]:
        raise AssertionError("recent live process should not be stopped")

    monkeypatch.setattr(orchestrator, "_stop_external_codex_process_tree", fail_stop)

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Recent live process without output",
            description="Acceptance task",
            depth=1,
            status=TaskStatus.ACCEPTING.value,
        )
        db.add_all([root, child])
        db.commit()

    result = orchestrator.cleanup_stalled_external_codex_agents(root_id)

    assert result["status"] == "ok"
    assert result["requeued"] == 0
    with SessionLocal() as db:
        task = db.get(TaskNode, child_id)
        assert task is not None
        assert task.status == TaskStatus.ACCEPTING.value


def test_watchdog_state_keeps_recent_timeout_noise_running(tmp_path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-agent-recent-timeout"
    child_id = "node-agent-recent-timeout"
    run_dir = tmp_path / "runs" / child_id
    run_dir.mkdir(parents=True)
    log_path = run_dir / "worker.jsonl"
    log_path.write_text(
        '{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"ok","exit_code":0,"status":"completed"}}\n'
        '{"type":"error","message":"Reconnecting... 1/5 (request timed out)"}\n',
        encoding="utf-8",
    )
    now = time.time()
    os.utime(log_path, (now, now))
    prompt_path = run_dir / "worker-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    os.utime(prompt_path, (now - 10, now - 10))

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Worker with recent timeout noise",
            description="Worker task",
            depth=1,
            status=TaskStatus.WORKING.value,
        )
        db.add_all([root, child])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    diagnostic = state["active_agent_diagnostics"][0]
    assert diagnostic["severity"] == "running"
    assert diagnostic["no_progress_seconds"] < 60
    assert "timeout" in diagnostic["markers"]
    assert "仅记录连接/重试信号" in diagnostic["summary"]


def test_frozen_watchdog_state_is_read_only(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        workspace_dir=tmp_path,
        frozen_mode=True,
        watchdog_orphan_resume_after_seconds=0,
    )
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="frozen-watchdog-root",
        title="Root",
        description="Should stay read-only.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
    )
    child = TaskNode(
        id="frozen-watchdog-child",
        parent_id=root.id,
        title="Queued",
        description="Queued child.",
        depth=1,
        status=TaskStatus.QUEUED.value,
    )
    with SessionLocal() as db:
        db.add_all([root, child])
        db.commit()

        def fail_recover(*_args, **_kwargs):
            raise AssertionError("frozen watchdog state must not recover messages")

        def fail_snapshot(*_args, **_kwargs):
            raise AssertionError("frozen watchdog state must not write snapshots")

        monkeypatch.setattr(orchestrator, "_recover_finished_agent_messages", fail_recover)
        monkeypatch.setattr(orchestrator, "_recover_finished_acceptance_messages", fail_recover)
        monkeypatch.setattr(orchestrator, "_write_state_snapshot", fail_snapshot)
        monkeypatch.setattr(orchestrator, "_maybe_write_full_checkpoint", fail_snapshot)

        state = orchestrator.get_watchdog_state(db, root_id=root.id)

    assert state["root_id"] == root.id
    assert state["active_frontier"] == 1
    assert state["orphaned_active_tree"] is False
    assert state["should_resume"] is False
    assert state["recommended_action"] == "observe"
    assert not (tmp_path / "l4_guardian_state.json").exists()
    assert not (tmp_path / "state_snapshots").exists()
    assert not (tmp_path / "full_checkpoints").exists()


def test_watchdog_state_throttles_snapshot_writes(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        workspace_dir=tmp_path,
        watchdog_state_snapshot_interval_seconds=300,
        full_checkpoint_enabled=True,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_external_codex_processes", lambda max_cache_age_seconds=5.0: [])
    snapshot_calls: list[tuple[str, str, bool]] = []
    checkpoint_calls: list[tuple[str, str]] = []

    def fake_snapshot(_db: object, root_id: str, reason: str, keep_history: bool = True) -> Path:
        snapshot_calls.append((root_id, reason, keep_history))
        latest_path = tmp_path / "state_snapshots" / root_id / "latest.json"
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        latest_path.write_text("{}", encoding="utf-8")
        return latest_path

    def fake_checkpoint(_db: object, root_id: str, reason: str) -> None:
        checkpoint_calls.append((root_id, reason))

    monkeypatch.setattr(orchestrator, "_write_state_snapshot", fake_snapshot)
    monkeypatch.setattr(orchestrator, "_maybe_write_full_checkpoint", fake_checkpoint)

    with SessionLocal() as db:
        root = TaskNode(
            id="snapshot-throttle-root",
            title="Root",
            description="Throttle hot-path snapshots.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        child = TaskNode(
            id="snapshot-throttle-child",
            parent_id=root.id,
            title="Child",
            description="Completed.",
            depth=1,
            status=TaskStatus.DONE.value,
        )
        db.add_all([root, child])
        db.commit()

        first = orchestrator.get_watchdog_state(db, root_id=root.id)
        second = orchestrator.get_watchdog_state(db, root_id=root.id)

    assert first["root_id"] == root.id
    assert second["root_id"] == root.id
    assert snapshot_calls == [(root.id, "watchdog.state", False)]
    assert checkpoint_calls == [(root.id, "watchdog.hourly")]


def test_watchdog_state_defers_full_checkpoint_while_topoff_needed(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        workspace_dir=tmp_path,
        watchdog_state_snapshot_interval_seconds=300,
        full_checkpoint_enabled=True,
        max_concurrency=13,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_external_codex_processes", lambda max_cache_age_seconds=5.0: [])
    snapshot_calls: list[tuple[str, str, bool]] = []
    checkpoint_calls: list[tuple[str, str]] = []

    def fake_snapshot(_db: object, root_id: str, reason: str, keep_history: bool = True) -> Path:
        snapshot_calls.append((root_id, reason, keep_history))
        latest_path = tmp_path / "state_snapshots" / root_id / "latest.json"
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        latest_path.write_text("{}", encoding="utf-8")
        return latest_path

    def fake_checkpoint(_db: object, root_id: str, reason: str) -> None:
        checkpoint_calls.append((root_id, reason))

    monkeypatch.setattr(orchestrator, "_write_state_snapshot", fake_snapshot)
    monkeypatch.setattr(orchestrator, "_maybe_write_full_checkpoint", fake_checkpoint)

    with SessionLocal() as db:
        root = TaskNode(
            id="snapshot-topoff-root",
            title="Root",
            description="Topoff should not wait for a full checkpoint.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        child = TaskNode(
            id="snapshot-topoff-child",
            parent_id=root.id,
            title="Child",
            description="Queued.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, child])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root.id)

    assert state["recommended_action"] == "top_off"
    assert snapshot_calls == [(root.id, "watchdog.state", False)]
    assert checkpoint_calls == []


def test_codex_slots_active_uses_longer_observation_cache(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path, max_concurrency=3)
    orchestrator = Orchestrator(settings)
    observed_cache_windows: list[float] = []

    def fake_external(max_cache_age_seconds: float = 2.0) -> list[dict[str, object]]:
        observed_cache_windows.append(max_cache_age_seconds)
        return [{"pid": 123, "task_id": "cached-process"}]

    monkeypatch.setattr(orchestrator, "_external_codex_processes", fake_external)

    assert orchestrator._codex_slots_active() == 1
    assert observed_cache_windows == [5.0]


def test_watchdog_state_tolerates_mixed_naive_and_aware_datetimes(tmp_path: Path):
    settings = get_settings().__class__(
        workspace_dir=tmp_path,
        watchdog_orphan_resume_after_seconds=0,
        full_checkpoint_enabled=False,
    )
    orchestrator = Orchestrator(settings)
    old_root = TaskNode(
        id="mixed-time-old-root",
        title="Old root",
        description="Old.",
        depth=0,
        status=TaskStatus.CANCELED.value,
        created_at=datetime(2026, 1, 1, 0, 0, 0),
        updated_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    root = TaskNode(
        id="mixed-time-root",
        title="Root",
        description="Mixed timestamps.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
        created_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
    )
    child = TaskNode(
        id="mixed-time-child",
        parent_id=root.id,
        title="Queued",
        description="Queued child.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        created_at=datetime(2026, 1, 2, 0, 1, 0),
        updated_at=datetime(2026, 1, 2, 0, 1, 0),
    )
    with SessionLocal() as db:
        db.add_all([old_root, root, child])
        db.commit()

        state = orchestrator.get_watchdog_state(db)

    assert state["root_id"] == root.id
    assert state["active_frontier"] == 1
    assert state["recommended_action"] == "resume_orphaned"


def test_watchdog_state_does_not_recover_finished_messages_on_read(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        workspace_dir=tmp_path,
        full_checkpoint_enabled=False,
    )
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="read-only-watchdog-root",
        title="Root",
        description="State endpoint must not mutate task structure.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
    )
    child = TaskNode(
        id="read-only-watchdog-child",
        parent_id=root.id,
        title="Queued",
        description="Queued child.",
        depth=1,
        status=TaskStatus.QUEUED.value,
    )
    with SessionLocal() as db:
        db.add_all([root, child])
        db.commit()

        def fail_recover(*_args, **_kwargs):
            raise AssertionError("watchdog state reads must not recover finished messages")

        monkeypatch.setattr(orchestrator, "_recover_finished_agent_messages", fail_recover)
        monkeypatch.setattr(orchestrator, "_recover_finished_acceptance_messages", fail_recover)

        state = orchestrator.get_watchdog_state(db, root_id=root.id)
        task_count = db.query(TaskNode).count()

    assert state["root_id"] == root.id
    assert task_count == 2


def test_recent_full_checkpoint_with_missing_artifacts_is_not_fresh(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        full_checkpoint_enabled=True,
        full_checkpoint_interval_seconds=3600,
    )
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="missing-artifact-checkpoint-root",
        title="Root",
        description="Checkpoint repair probe.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
    )

    with SessionLocal() as db:
        db.add(root)
        db.commit()
        checkpoint_root = orchestrator._full_checkpoint_root(root.id)
        missing_dir = checkpoint_root / "missing"
        checkpoint_root.mkdir(parents=True)
        (checkpoint_root / "latest.json").write_text(
            json.dumps(
                {
                    "checkpoint_version": 1,
                    "root_id": root.id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "database_copy": str(missing_dir / "battlefield.sqlite3"),
                    "target_copy": str(missing_dir / "target"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        manifest_path = orchestrator._maybe_write_full_checkpoint(db, root.id, "watchdog.hourly")

    assert manifest_path is not None
    assert manifest_path.is_file()
    assert manifest_path.parent != missing_dir
    latest = json.loads((checkpoint_root / "latest.json").read_text(encoding="utf-8"))
    assert latest["database_copy"] != str(missing_dir / "battlefield.sqlite3")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for checkpoint head-change tests")
def test_recent_full_checkpoint_is_refreshed_when_battlefield_head_changed(monkeypatch, tmp_path: Path):
    repo = tmp_path / "battlefield-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test Snapshotter"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "snapshotter@example.local"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "tool.txt").write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "add", "tool.txt"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "First"], cwd=repo, check=True, capture_output=True, text=True)
    first_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    (repo / "tool.txt").write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "add", "tool.txt"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Second"], cwd=repo, check=True, capture_output=True, text=True)

    monkeypatch.setattr(orchestrator_module, "ROOT_DIR", repo)
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        full_checkpoint_enabled=True,
        checkpoint_ledger_enabled=True,
        full_checkpoint_interval_seconds=3600,
    )
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="head-change-checkpoint-root",
        title="Root",
        description="Checkpoint should refresh on tool HEAD change.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
    )

    with SessionLocal() as db:
        db.add(root)
        db.commit()
        checkpoint_root = orchestrator._full_checkpoint_root(root.id)
        stale_dir = checkpoint_root / "20260618T010203000000Z"
        stale_dir.mkdir(parents=True)
        db_copy = stale_dir / "battlefield.sqlite3"
        db_copy.write_bytes(b"stale-db")
        manifest_path = stale_dir / "manifest.json"
        stale_manifest = {
            "checkpoint_version": 1,
            "root_id": root.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "checkpoint_manifest": str(manifest_path),
            "database_copy": str(db_copy),
        }
        manifest_path.write_text(json.dumps(stale_manifest), encoding="utf-8")
        (checkpoint_root / "latest.json").write_text(json.dumps(stale_manifest), encoding="utf-8")
        ledger_dir = settings.workspace_dir / "checkpoint_ledger" / root.id
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "latest.json").write_text(
            json.dumps(
                {
                    "checkpoint_id": stale_dir.name,
                    "battlefield_git_commit_before": first_head,
                }
            ),
            encoding="utf-8",
        )

        refreshed = orchestrator._maybe_write_full_checkpoint(db, root.id, "watchdog.head_changed")

    assert refreshed is not None
    assert refreshed.parent.name != stale_dir.name


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason="checkpoint ledger 用例需要真的调 git（与上面两条同类用例保持一致）",
)
def test_full_checkpoint_writes_lightweight_git_ledger(tmp_path: Path):
    workspace = tmp_path / "workspace"
    target = tmp_path / "target-godot"
    target.mkdir()
    (target / "project.godot").write_text("[application]\nconfig/name=\"Probe\"\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test Snapshotter"], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "snapshotter@example.local"], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "project.godot"], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial target"], cwd=target, check=True, capture_output=True, text=True)
    (target / "README.md").write_text("dirty target state\n", encoding="utf-8")

    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=workspace,
        full_checkpoint_enabled=True,
        checkpoint_ledger_enabled=True,
    )
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="ledger-checkpoint-root",
        title="Root",
        description="Checkpoint ledger probe.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
        scope_json=dumps([str(target)]),
    )

    with SessionLocal() as db:
        db.add(root)
        db.commit()
        manifest_path = orchestrator._write_full_checkpoint(db, root.id, "watchdog.hourly")

    assert manifest_path is not None
    checkpoint_id = manifest_path.parent.name
    ledger_path = workspace / "checkpoint_ledger" / root.id / f"{checkpoint_id}.json"
    latest_ledger_path = workspace / "checkpoint_ledger" / root.id / "latest.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    latest_ledger = json.loads(latest_ledger_path.read_text(encoding="utf-8"))
    assert ledger["version_id"] == f"{root.id}-{checkpoint_id}"
    assert latest_ledger["checkpoint_id"] == checkpoint_id
    assert ledger["checkpoint_manifest_sha256"]
    assert ledger["database_copy_sha256"]
    assert ledger["target_source"] == str(target)
    assert ledger["target_git"]["git_ready"] is True
    assert ledger["target_git"]["dirty"] is True
    assert ledger["battlefield_git"]["git_ready"] is True


def test_watchdog_state_reports_quiet_running_agent_without_claiming_recent_output(tmp_path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-agent-quiet"
    child_id = "node-agent-quiet"
    run_dir = tmp_path / "runs" / child_id
    run_dir.mkdir(parents=True)
    log_path = run_dir / "worker.jsonl"
    log_path.write_text(
        '{"type":"thread.started","thread_id":"quiet"}\n'
        '{"type":"turn.started"}\n',
        encoding="utf-8",
    )
    old_timestamp = time.time() - 700
    os.utime(log_path, (old_timestamp, old_timestamp))
    prompt_path = run_dir / "worker-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    os.utime(prompt_path, (old_timestamp - 10, old_timestamp - 10))

    with SessionLocal() as db:
        root = TaskNode(
            id=root_id,
            title="Root",
            description="Root task",
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Quiet worker",
            description="Worker task",
            depth=1,
            status=TaskStatus.WORKING.value,
        )
        db.add_all([root, child])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    diagnostic = state["active_agent_diagnostics"][0]
    assert diagnostic["task_id"] == child_id
    assert diagnostic["severity"] == "warning"
    assert diagnostic["no_progress_seconds"] >= 600
    assert "没有日志或输出文件更新" in diagnostic["summary"]
    assert "最近仍有日志或输出文件活动" not in diagnostic["summary"]


def test_watchdog_state_ignores_repeated_codex_environment_noise_as_progress(tmp_path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    root_id = "root-agent-noise"
    child_id = "node-agent-noise"
    run_dir = tmp_path / "runs" / child_id
    run_dir.mkdir(parents=True)
    old_progress_at = datetime.now(timezone.utc) - timedelta(seconds=700)
    recent_noise_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    log_path = run_dir / "worker.jsonl"
    log_path.write_text(
        f'{old_progress_at.isoformat().replace("+00:00", "Z")} INFO real command output\n'
        f'{recent_noise_at.isoformat().replace("+00:00", "Z")}  WARN codex_core_skills::loader: ignoring interface.icon_small: icon path with ".." must resolve under plugin assets/\n'
        f'{recent_noise_at.isoformat().replace("+00:00", "Z")}  WARN codex_core_plugins::manifest: ignoring interface.defaultPrompt[0]: prompt must be at most 128 characters\n',
        encoding="utf-8",
    )
    now = time.time()
    os.utime(log_path, (now, now))
    prompt_path = run_dir / "worker-prompt.txt"
    prompt_path.write_text("prompt", encoding="utf-8")
    os.utime(prompt_path, (now - 710, now - 710))

    with SessionLocal() as db:
        root = TaskNode(id=root_id, title="Root", description="Root task", status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(
            id=child_id,
            parent_id=root_id,
            title="Noisy worker",
            description="Worker task",
            depth=1,
            status=TaskStatus.WORKING.value,
        )
        db.add_all([root, child])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    diagnostic = state["active_agent_diagnostics"][0]
    assert diagnostic["severity"] == "warning"
    assert diagnostic["no_progress_seconds"] >= 600
    assert "codex_core_skills::loader" not in diagnostic["tail_summary"]


def test_real_runner_ignores_codex_environment_noise_for_activity():
    noisy_text = (
        "2026-06-17T23:09:07.740533Z  WARN codex_core_skills::loader: ignoring interface.icon_small: icon path with '..' must resolve under plugin assets/\n"
        "2026-06-17T23:09:10.531643Z  WARN codex_core_plugins::manifest: ignoring interface.defaultPrompt[0]: prompt must be at most 128 characters\n"
        "2026-06-18T05:25:32.245456Z ERROR codex_models_manager::manager: failed to refresh available models: timeout waiting for child process to exit\n"
        "2026-06-18T05:24:38.369944Z  WARN codex_analytics::client: failed to send events request: error sending request for url (https://chatgpt.com/backend-api/codex/analytics-events/events)\n"
        "2026-06-18T05:48:28.510630Z  WARN codex_core::hook_runtime: after_agent hook failed; continuing turn_id=abc hook_name=legacy_notify error=file name too long\n"
        "2026-06-18T05:48:28.511911Z  WARN codex_mcp::rmcp_client: failed to initialize MCP client during shutdown: MCP startup failed: timed out awaiting tools/list after 30s\n"
    )
    meaningful_text = noisy_text + '{"type":"item.started","item":{"type":"command_execution","status":"in_progress"}}\n'

    assert not is_meaningful_codex_activity_text(noisy_text)
    assert is_meaningful_codex_activity_text(meaningful_text)


def test_real_runner_extracts_worker_needs_decomposition_from_stdout_event(tmp_path):
    runner = CodexExecRunner(get_settings().__class__(workspace_dir=tmp_path))
    payload = {
        "status": "needs_decomposition",
        "summary": "split this parent",
        "changed_files": [],
        "tests": [],
        "evidence": ["not leaf safe"],
        "risks": ["too broad"],
        "suggested_children": [
            {
                "title": "Inventory sources",
                "description": "Collect source files.",
                "scope": ["docs"],
                "acceptance": ["source index exists"],
                "depends_on": [],
                "coverage_ids": ["source_files"],
                "verification_kind": "json_check",
                "estimated_leaf": True,
            }
        ],
    }
    line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}})

    extracted = runner._early_structured_payload_from_stdout_line(line, "worker")

    assert extracted is not None
    assert extracted["status"] == "needs_decomposition"
    assert extracted["suggested_children"][0]["title"] == "Inventory sources"


def test_real_runner_does_not_early_extract_worker_success_from_stdout_event(tmp_path):
    runner = CodexExecRunner(get_settings().__class__(workspace_dir=tmp_path))
    payload = {
        "status": "succeeded",
        "summary": "keep running until final CLI exit",
        "changed_files": ["game.tscn"],
        "tests": ["ok"],
        "evidence": ["done"],
        "risks": [],
        "suggested_children": [],
    }
    line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}})

    assert runner._early_structured_payload_from_stdout_line(line, "worker") is None
    assert runner._early_structured_payload_from_stdout_line(line, "planner") is None


def test_real_runner_appends_log_chunks_incrementally(tmp_path):
    log_path = tmp_path / "worker.jsonl"
    log_path.write_bytes(b"header\n")

    CodexExecRunner._append_log_chunk(log_path, b"chunk-1\n")
    CodexExecRunner._append_log_chunk(log_path, b"chunk-2\n")

    assert log_path.read_bytes() == b"header\nchunk-1\nchunk-2\n"


@pytest.mark.asyncio
async def test_real_runner_writes_attempt_header_before_subprocess_communication(monkeypatch, tmp_path):
    runner = CodexExecRunner(get_settings().__class__(workspace_dir=tmp_path, codex_retry_attempts=0))

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return object()

    async def fake_communicate(process, prompt_bytes, output_path, logs_path, activity_path, log_chunks, kind):
        assert process is not None
        assert prompt_bytes
        assert kind == "planner"
        assert activity_path.name == "planner-activity.txt"
        assert logs_path.read_text(encoding="utf-8") == "\n--- codex planner attempt 1/1 ---\n"
        payload = PlannerDecision(decision="execute", reason="ok", leaf_certainty="300%").model_dump()
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return 0, b"", None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(runner, "_communicate_with_watchdog", fake_communicate)

    envelope = await runner._run_codex_io(
        run_id="attempt-header-node",
        cwd=tmp_path,
        kind="planner",
        prompt="Plan this.",
        schema_name="planner-decision.schema.json",
        sandbox="read-only",
    )

    assert envelope.exit_code == 0
    assert envelope.payload["decision"] == "execute"


def test_agent_log_markers_ignore_watchdog_config_and_timestamp_429(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-false-positive" / "planner.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n--- codex planner watchdog: hard_timeout=disabled, idle_timeout=1800s ---\n"
        "2026-06-17T15:05:50.434429Z  WARN codex_core_skills::loader: ignoring icon\n",
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "timeout" not in markers
    assert "resource_limit" not in markers


def test_agent_log_markers_ignore_rg_line_number_429(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-rg-line-number" / "verifier.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"aggregated_output":"426:StreetThug\\n428:StreetThug\\n429:StreetThug\\n430:StreetThug\\n"}}\n',
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "resource_limit" not in markers


def test_agent_log_markers_ignore_rg_line_number_429_inside_completed_jsonl(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-rg-jsonl-line-number" / "verifier.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"command":"rg -n -F \\"scripts/actors/enemies/StreetThugEnemy.gd\\" docs/audit.json",'
        '"aggregated_output":"428:        \\"scripts/actors/enemies/StreetThugEnemy.gd:4\\",\\n'
        '429:        \\"scripts/actors/enemies/StreetThugEnemy.gd:5\\",\\n'
        '446:      \\"resource_exists_now\\": false,\\n",'
        '"exit_code":0,"status":"completed"}}\n',
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "resource_limit" not in markers


def test_agent_log_markers_ignore_reconnect_delay_429ms(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-reconnect-delay" / "worker.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "2026-06-18T19:26:18.840147Z WARN codex_core::responses_retry: "
        "stream disconnected - retrying sampling request (2/5 in 429ms)...\n"
        '{"type":"error","message":"Reconnecting... 2/5 (request timed out)"}\n',
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "reconnecting" in markers
    assert "timeout" in markers
    assert "resource_limit" not in markers


def test_agent_log_markers_ignore_agent_message_usage_limit_evidence(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-agent-message-history" / "verifier.jsonl"
    log_path.parent.mkdir(parents=True)
    payload = {
        "status": "succeeded",
        "summary": "Historical diagnosis mentions: You've hit your usage limit.",
        "evidence": ["turn.failed: usage limit at 1:24 AM was already handled."],
    }
    log_path.write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(payload)}}),
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "resource_limit" not in markers


def test_agent_log_markers_ignore_shell_tool_output_usage_limit_evidence(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-shell-output-history" / "worker.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "2026-06-19T00:42:46.621163Z ERROR codex_core::tools::router: error=Exit code: 1 "
        'docs\\skill_notes\\node-a.json:31: "planner_misclassification_risk": '
        '"If a parent only sees code 1 and does not parse the usage limit line, it may misclassify the blocker."\n',
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "resource_limit" not in markers


def test_agent_log_markers_ignore_shell_tool_aggregated_output_usage_limit_quote(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-shell-output-history-quote" / "worker.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        '2026-06-19T03:28:27.264873Z ERROR codex_core::tools::router: error=Exit code: 1 Wall time: 1.1 seconds '
        'Output: {"type":"item.completed","item":{"type":"command_execution","command":"rg docs",'
        '"aggregated_output":"docs/recovery/node-a.json:20: '
        "\\\"error_message\\\": \\\"You've hit your usage limit. Visit settings or try again at 11:25 AM.\\\""
        '","exit_code":0,"status":"completed"}}\n',
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "resource_limit" not in markers


def test_agent_log_markers_ignore_partial_tail_inside_command_output_usage_limit_quote(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-partial-command-tail" / "worker.jsonl"
    log_path.parent.mkdir(parents=True)
    historical_output = (
        "x" * 9000
        + " docs/skill_notes/node-a.json:19: You've hit your usage limit. "
        + "This is historical evidence being searched by rg, not a live Codex error."
    )
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_19",
                    "type": "command_execution",
                    "command": 'rg -n -F "usage limit" docs/recovery docs/skill_notes docs/blockers',
                    "aggregated_output": historical_output,
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_20",
                    "type": "command_execution",
                    "command": "python validate.py",
                    "aggregated_output": "ok",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "resource_limit" not in markers


def test_agent_log_markers_detect_real_turn_failed_after_long_command_output(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-real-error-after-long-output" / "worker.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "aggregated_output": "x" * 9000,
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "turn.failed",
                "error": {
                    "message": "You've hit your usage limit. Visit settings or try again at 11:25 AM.",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "resource_limit" in markers


def test_agent_log_markers_ignore_codex_mcp_shutdown_tools_list_timeout(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-mcp-shutdown-noise" / "planner.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "WARN codex_mcp::rmcp_client: failed to initialize MCP client during shutdown: "
        "MCP startup failed: timed out awaiting tools/list after 30s\n",
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "timeout" not in markers
    assert "resource_limit" not in markers


def test_agent_log_markers_detect_real_timeout_and_status_429(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-real-pressure" / "planner.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        '2026-06-17T15:06:42Z {"type":"error","message":"Reconnecting... 2/5 (request timed out)"}\n'
        "HTTP status 429: too many requests\n",
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "timeout" in markers
    assert "resource_limit" in markers


def test_agent_log_markers_detect_turn_failed_usage_limit(tmp_path):
    orchestrator = Orchestrator(get_settings().__class__(workspace_dir=tmp_path))
    log_path = tmp_path / "runs" / "node-marker-turn-failed-usage" / "planner.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        json.dumps(
            {
                "type": "turn.failed",
                "error": {
                    "message": "You've hit your usage limit. Visit settings or try again at 1:24 AM.",
                },
            }
        ),
        encoding="utf-8",
    )

    markers, _summary = orchestrator._agent_log_markers(log_path)

    assert "resource_limit" in markers


@pytest.mark.asyncio
async def test_resume_hibernation_preserves_waiting_parent_and_requeues_active_child():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    constitution_id = await locked_constitution(orchestrator, "Build a status-sensitive hibernation probe.")
    root_id = await orchestrator.start_project(
        goal="Build a status-sensitive hibernation probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.status = TaskStatus.WAITING_CHILDREN.value
        child = TaskNode(
            id="resume-active-child",
            parent_id=root_id,
            title="Interrupted acceptance child",
            description="Interrupted while accepting.",
            depth=1,
            status=TaskStatus.ACCEPTING.value,
        )
        db.add(child)
        db.commit()

    await orchestrator.hibernate_tree(root_id, 60, "等待 token 池刷新")
    await orchestrator.resume_hibernated_tree(root_id)

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        child = db.get(TaskNode, "resume-active-child")
        assert root is not None
        assert child is not None
        assert root.status == TaskStatus.WAITING_CHILDREN.value
        assert child.status == TaskStatus.QUEUED.value

    tasks = list(orchestrator._auto_resume_tasks.values()) + list(orchestrator._background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_hibernate_schedules_auto_resume_before_background_cleanup_finishes():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    constitution_id = await locked_constitution(orchestrator, "Build a disconnect-safe hibernation probe.")
    root_id = await orchestrator.start_project(
        goal="Build a disconnect-safe hibernation probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )
    cancel_seen = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def slow_background_cleanup() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancel_seen.set()
            await release_cleanup.wait()
            raise

    background_task = asyncio.create_task(slow_background_cleanup())
    orchestrator._background_tasks.add(background_task)
    background_task.add_done_callback(orchestrator._background_tasks.discard)

    hibernate_task = asyncio.create_task(orchestrator.hibernate_tree(root_id, 60, "等待 token 池刷新"))
    await asyncio.wait_for(cancel_seen.wait(), timeout=30)

    assert root_id in orchestrator._auto_resume_tasks
    with SessionLocal() as db:
        task = db.get(TaskNode, root_id)
        assert task is not None
        assert task.status == TaskStatus.HIBERNATING.value

    release_cleanup.set()
    await asyncio.gather(hibernate_task, return_exceptions=True)
    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)


@pytest.mark.asyncio
async def test_startup_restores_hibernation_auto_resume_schedule():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    orchestrator = Orchestrator(settings)
    constitution_id = await locked_constitution(orchestrator, "Build a startup wakeup probe.")
    root_id = await orchestrator.start_project(
        goal="Build a startup wakeup probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )

    await orchestrator.hibernate_tree(root_id, 3600, "等待 token 池刷新")
    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)

    restarted = Orchestrator(settings)
    restored = restarted.schedule_hibernation_wakeups()

    assert restored == 1
    assert root_id in restarted._auto_resume_tasks
    scheduled = list(restarted._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)


def test_latest_root_id_prefers_active_root_over_newer_terminal_root():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    active_root = TaskNode(
        id="root-active",
        title="Active",
        description="Still running.",
        depth=0,
        status=TaskStatus.HIBERNATING.value,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer_done_root = TaskNode(
        id="root-done",
        title="Done",
        description="Already finished.",
        depth=0,
        status=TaskStatus.DONE.value,
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert orchestrator._latest_root_id([active_root, newer_done_root]) == "root-active"


def test_production_database_drop_guard_blocks_default_workspace_db(monkeypatch):
    """老的仓库内工作区库仍然受保护（迁移期还在用）。"""
    production_url = f"sqlite:///{(Path(__file__).resolve().parents[2] / 'workspace' / 'battlefield.sqlite3').as_posix()}"

    assert _should_block_destructive_sql(production_url, "DROP TABLE task_nodes") is True
    assert _should_block_destructive_sql(production_url, "select * from task_nodes") is False

    monkeypatch.setenv("CODEX_BATTLEFIELD_ALLOW_PRODUCTION_SCHEMA_DROP", "1")
    assert _should_block_destructive_sql(production_url, "DROP TABLE task_nodes") is False


def test_production_database_drop_guard_blocks_a_state_home_project_db(monkeypatch):
    """**H3 回归**：工程状态目录里的库也必须受保护。

    旧判据是"路径 == `<repo>/workspace/battlefield.sqlite3` 才算生产库"。
    状态与工程解耦后库落到 `<state_home>/projects/<key>/`，与那条硬编码路径
    **永远不相等** -> `DROP TABLE` 保护在**真正的活库**上静默失效。
    它不报错，只是不再保护——所以必须有一条断言盯着这个位置。
    """
    state_home = Path(os.path.expanduser("~")) / ".codex-battlefield"
    project_db = state_home / "projects" / "d--work-g10--deadbeef" / "battlefield.sqlite3"
    url = f"sqlite:///{project_db.as_posix()}"

    assert _should_block_destructive_sql(url, "DROP TABLE task_nodes") is True, (
        "工程状态目录里的库没有被保护——DROP TABLE 会在活库上静默生效"
    )
    monkeypatch.setenv("CODEX_BATTLEFIELD_ALLOW_PRODUCTION_SCHEMA_DROP", "1")
    assert _should_block_destructive_sql(url, "DROP TABLE task_nodes") is False


def test_production_database_drop_guard_blocks_an_env_selected_db(monkeypatch, tmp_path):
    """`CODEX_BATTLEFIELD_DATABASE_URL` 指定的任意位置同样受保护（不靠路径特征）。

    注意位置必须**真的在临时目录之外**：第一版写成 `tmp_path.parent / ...`，
    而 `tmp_path.parent` 仍然在临时目录里，于是被判成"可丢"——
    测试自己把要证的事情否掉了。
    """
    custom = Path(os.path.expanduser("~")) / ".codex-battlefield-guard-probe" / "battlefield.sqlite3"
    url = f"sqlite:///{custom.as_posix()}"
    assert _should_block_destructive_sql(url, "DROP TABLE task_nodes") is True


def test_production_database_drop_guard_allows_test_databases(tmp_path: Path):
    test_url = f"sqlite:///{(tmp_path / 'codex_battlefield_test.sqlite3').as_posix()}"

    assert _should_block_destructive_sql(test_url, "DROP TABLE task_nodes") is False


def test_watchdog_install_command_reserves_agent_start_budget(monkeypatch):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        agent_burst_limit=8,
        agent_burst_reserve=2,
        agent_burst_resume_after_seconds=18000,
    )
    orchestrator = Orchestrator(settings)
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        class Completed:
            returncode = 0
            stdout = "installed"
            stderr = ""

        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = orchestrator.install_watchdog_hook("root-token-reserve")

    args = captured["args"]
    assert isinstance(args, list)
    assert result["status"] == "succeeded"
    assert result["root_id"] == "root-token-reserve"
    assert "register_battlefield_watchdog_task.ps1" in result["command"]
    assert "-RootId" in args
    assert "root-token-reserve" in args
    assert "-AgentBurstLimit" in args
    assert "8" in args
    assert "-AgentBurstReserve" in args
    assert "2" in args
    assert "-WithLogonTrigger" in args
    assert captured["kwargs"]["timeout"] == 90


def test_watchdog_install_command_goal_mode_disables_agent_start_budget(monkeypatch):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        goal_mode=True,
        agent_burst_limit=8,
        agent_burst_reserve=2,
        agent_burst_resume_after_seconds=18000,
    )
    orchestrator = Orchestrator(settings)
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        class Completed:
            returncode = 0
            stdout = "installed"
            stderr = ""

        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = orchestrator.install_watchdog_hook("root-goal-mode")

    args = captured["args"]
    assert isinstance(args, list)
    assert result["status"] == "succeeded"
    assert "-GoalMode" in args
    assert "-AgentBurstLimit" in args
    assert args[args.index("-AgentBurstLimit") + 1] == "0"
    assert "-AgentBurstReserve" in args
    assert args[args.index("-AgentBurstReserve") + 1] == "0"


def test_watchdog_command_output_decodes_windows_native_chinese(monkeypatch):
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))

    def fake_run(*_args, **_kwargs):
        class Completed:
            returncode = 0
            stdout = "成功: 成功创建了计划任务\nInstalled hook\n".encode("cp936")
            stderr = "警告: 使用 Startup fallback\n".encode("cp936")

        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = orchestrator._run_watchdog_command(["powershell.exe", "-File", "hook.ps1"], "root-cn-output", timeout_seconds=10)

    assert result["status"] == "succeeded"
    assert "成功创建" in result["stdout"]
    assert "Startup fallback" in result["stderr"]
    assert "�" not in result["stdout"]
    assert "�" not in result["stderr"]


def test_watchdog_hook_state_reports_windows_task_files(tmp_path: Path, monkeypatch):
    system_root = tmp_path / "Windows"
    task_dir = system_root / "System32" / "Tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "CodexBattlefieldWatchdog").write_text("<Task />", encoding="utf-8")

    appdata = tmp_path / "AppData" / "Roaming"
    startup_dir = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True)
    (startup_dir / "CodexBattlefieldWatchdog-Logon.cmd").write_text("@echo off\n", encoding="ascii")

    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setenv("APPDATA", str(appdata))

    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        agent_burst_limit=8,
        agent_burst_reserve=1,
    )
    orchestrator = Orchestrator(settings)

    state = orchestrator._watchdog_hook_state("root-token-reserve")

    assert state["scheduled_task_exists"] is True
    assert state["scheduled_task_path"].endswith("CodexBattlefieldWatchdog")
    assert state["watchdog_interval_minutes"] == 1
    assert "-IntervalMinutes 1" in state["watchdog_install_command"]
    assert state["logon_task_exists"] is False
    assert state["startup_hook_exists"] is True
    assert state["startup_hook_mode"] == "startup_folder"
    assert "额外保留 1 次预算" in state["token_reserve_summary"]
    assert state["codex_health_exists"] is False
    assert state["codex_health_status"] == "unknown"
    assert state["codex_quota_observability"] == "codex_cli_has_no_direct_quota_or_5h_reset_api"
    assert state["api_restart_pending"] is False
    assert state["api_restart_flag_path"].endswith("restart_api_when_idle.flag")
    assert state["api_restart_drain_armed"] is False
    assert state["api_restart_drain_flag_path"].endswith("restart_api_drain_when_safe.flag")


def test_watchdog_hook_state_reads_codex_health_snapshot(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "codex_health.json").write_text(
        json.dumps(
            {
                "checked_at": "2026-06-17T12:00:00+00:00",
                "status": "warning",
                "summary": "Codex 已登录；doctor 超时，仅作为连通性告警。",
                "quota_observability": "retry_after_observed_from_codex_output",
                "quota_retry_after_seconds": 3600,
                "quota_reset_at": "2026-06-17T13:00:00+00:00",
                "quota_retry_after_source": "codex_output",
                "quota_signal": "retry-after: 3600",
                "quota_source": "doctor",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=workspace)
    orchestrator = Orchestrator(settings)

    state = orchestrator._watchdog_hook_state("root-health")

    assert state["codex_health_exists"] is True
    assert state["codex_health_status"] == "warning"
    assert state["codex_health_checked_at"].isoformat() == "2026-06-17T12:00:00+00:00"
    assert "doctor 超时" in state["codex_health_summary"]
    assert state["codex_quota_observability"] == "retry_after_observed_from_codex_output"
    assert state["codex_quota_retry_after_seconds"] == 3600
    assert state["codex_quota_reset_at"].isoformat() == "2026-06-17T13:00:00+00:00"
    assert state["codex_quota_retry_after_source"] == "codex_output"
    assert state["codex_quota_signal"] == "retry-after: 3600"
    assert state["codex_quota_source"] == "doctor"
    assert state["codex_health_path"].endswith("codex_health.json")


def test_watchdog_hook_state_downgrades_elapsed_codex_retry_after(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reset_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    (workspace / "codex_health.json").write_text(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "resource_block",
                "summary": "codex_runtime_retry_after_observed",
                "quota_observability": "retry_after_observed_from_codex_output",
                "quota_retry_after_seconds": 3600,
                "quota_reset_at": reset_at,
                "quota_retry_after_source": "codex_output",
                "quota_signal": "retry-after: 3600",
                "quota_source": "run_log:runs/node-a/worker.jsonl",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=workspace)
    orchestrator = Orchestrator(settings)

    state = orchestrator._watchdog_hook_state("root-health-expired")

    assert state["codex_health_status"] == "warning"
    assert state["codex_quota_observability"] == "retry_after_elapsed_from_codex_output"
    assert state["codex_quota_retry_after_seconds"] == 0
    assert state["codex_quota_reset_at"].isoformat() == reset_at


def test_watchdog_hook_state_reports_pending_idle_api_restart(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "restart_api_when_idle.flag").write_text("load latest code when idle\n", encoding="ascii")
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=workspace)
    orchestrator = Orchestrator(settings)

    state = orchestrator._watchdog_hook_state("root-restart")

    assert state["api_restart_pending"] is True
    assert state["api_restart_flag_path"].endswith("restart_api_when_idle.flag")
    assert state["api_restart_drain_armed"] is False


def test_watchdog_hook_state_reports_armed_api_restart_drain(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "restart_api_when_idle.flag").write_text("load latest code when idle\n", encoding="ascii")
    (workspace / "restart_api_drain_when_safe.flag").write_text("allow short hibernation for maintenance restart\n", encoding="ascii")
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=workspace)
    orchestrator = Orchestrator(settings)

    state = orchestrator._watchdog_hook_state("root-restart")

    assert state["api_restart_pending"] is True
    assert state["api_restart_drain_armed"] is True
    assert state["api_restart_drain_flag_path"].endswith("restart_api_drain_when_safe.flag")


def test_set_api_restart_drain_arms_and_disarms_flags(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=workspace)
    orchestrator = Orchestrator(settings)

    armed = orchestrator.set_api_restart_drain(True)

    assert armed["status"] == "armed"
    assert armed["api_restart_pending"] is True
    assert armed["api_restart_drain_armed"] is True
    assert (workspace / "restart_api_when_idle.flag").is_file()
    assert (workspace / "restart_api_drain_when_safe.flag").is_file()

    disarmed = orchestrator.set_api_restart_drain(False)

    assert disarmed["status"] == "disarmed"
    assert disarmed["api_restart_pending"] is True
    assert disarmed["api_restart_drain_armed"] is False
    assert (workspace / "restart_api_when_idle.flag").is_file()
    assert not (workspace / "restart_api_drain_when_safe.flag").exists()


def test_scheduler_prioritizes_final_product_gate_repairs_before_general_queue(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    created_at = datetime(2026, 6, 17, tzinfo=timezone.utc)
    general = TaskNode(
        id="general",
        title="普通排队任务",
        description="A regular queued task.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["implementation"]}),
        created_at=created_at,
    )
    final_gate = TaskNode(
        id="final",
        title="最终成品门禁修复：补齐敌人和 Boss 场景",
        description="Repair final gate.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["final-product-gate", "brawler-enemy-scenes"]}),
        created_at=created_at + timedelta(seconds=10),
    )
    skill_note = TaskNode(
        id="skill",
        title="父节点：归拢递归经验 Skill",
        description="Consolidate notes.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["codex-battlefield-skill-note"]}),
        created_at=created_at - timedelta(seconds=10),
    )
    pending = TaskNode(
        id="pending",
        title="已有 worker 结果待验收",
        description="Resume verifier.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["implementation"]}),
        created_at=created_at + timedelta(seconds=20),
    )

    ordered = sorted(
        [general, final_gate, skill_note, pending],
        key=lambda task: orchestrator._scheduler_ready_priority(task, has_pending_worker_result=task.id == "pending"),
    )

    assert [task.id for task in ordered] == ["pending", "final", "general", "skill"]


def test_scheduler_deprioritizes_legacy_skill_note_titles(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    created_at = datetime(2026, 6, 17, tzinfo=timezone.utc)
    general = TaskNode(
        id="general",
        title="Implement enemy scene contract",
        description="A regular queued task.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["implementation"]}),
        created_at=created_at + timedelta(seconds=10),
    )
    legacy_skill_note = TaskNode(
        id="legacy-skill-note",
        title="\u6c47\u603b\u672c\u8282\u70b9\u6280\u80fd\u7b14\u8bb0",
        description="Legacy title-only skill note task.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["implementation"]}),
        created_at=created_at,
    )
    self_rescue = TaskNode(
        id="self-rescue",
        title="Skill/KM self rescue and blocker attribution",
        description="A recovery task that mentions Skill but is not a note merge.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["implementation"]}),
        created_at=created_at - timedelta(seconds=10),
    )

    ordered = sorted([legacy_skill_note, general], key=lambda task: orchestrator._scheduler_ready_priority(task))

    assert orchestrator._is_skill_note_task(legacy_skill_note)
    assert not orchestrator._is_skill_note_task(self_rescue)
    assert orchestrator._is_skill_note_spec(ChildTaskSpec(title="\u5f52\u5e76\u672c\u8f6e\u9012\u5f52\u7ecf\u9a8c Skill \u8bb0\u5f55", description="Merge notes."))
    assert [task.id for task in ordered] == ["general", "legacy-skill-note"]


def test_scheduler_deprioritizes_final_gate_skill_note_titles(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    created_at = datetime(2026, 6, 17, tzinfo=timezone.utc)
    final_gameplay = TaskNode(
        id="final-gameplay",
        title="Implement HUD life score and wave feedback",
        description="Actual final gate gameplay/UI work.",
        depth=10,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["final-product-gate", "hud_life_score_combo"]}),
        created_at=created_at + timedelta(seconds=20),
    )
    general = TaskNode(
        id="general",
        title="Implement regular enemy integration",
        description="Regular implementation task.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["implementation"]}),
        created_at=created_at + timedelta(seconds=10),
    )
    final_skill_note = TaskNode(
        id="final-skill-note",
        title="Blocked recovery Skill Note summary",
        description="Final lineage note aggregation, not gameplay implementation.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["final-product-gate", "parent-recovery-advice"]}),
        created_at=created_at,
    )

    ordered = sorted([final_skill_note, general, final_gameplay], key=lambda task: orchestrator._scheduler_ready_priority(task))

    assert orchestrator._is_final_product_gate_task(final_skill_note)
    assert orchestrator._is_skill_note_task(final_skill_note)
    assert [task.id for task in ordered] == ["final-gameplay", "general", "final-skill-note"]


def test_scheduler_prioritizes_final_gate_blocked_child_repair_even_with_skill_title(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    created_at = datetime(2026, 6, 17, tzinfo=timezone.utc)
    final_gameplay = TaskNode(
        id="final-gameplay",
        title="Implement final HUD evidence",
        description="Actual final gate gameplay/UI work.",
        depth=2,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["final-product-gate", "hud_life_score_combo"]}),
        created_at=created_at + timedelta(seconds=10),
    )
    final_blocked_repair_with_skill_title = TaskNode(
        id="final-blocked-repair",
        title="Repair or replace blocked child result: recursive Skill note branch",
        description="Integrate a replacement for a blocked final-gate child.",
        depth=3,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["final-product-gate", "blocked-child-repair", "node-old-skill"]}),
        created_at=created_at + timedelta(seconds=20),
    )
    general = TaskNode(
        id="general",
        title="Implement regular enemy integration",
        description="Regular implementation task.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"coverage_ids": ["implementation"]}),
        created_at=created_at,
    )

    ordered = sorted(
        [general, final_gameplay, final_blocked_repair_with_skill_title],
        key=lambda task: orchestrator._scheduler_ready_priority(task),
    )

    assert orchestrator._is_final_product_gate_task(final_blocked_repair_with_skill_title)
    assert orchestrator._is_skill_note_task(final_blocked_repair_with_skill_title)
    assert [task.id for task in ordered] == ["final-blocked-repair", "final-gameplay", "general"]


def test_final_product_gate_lineage_is_inherited_by_created_children(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    parent = TaskNode(
        id="parent-final",
        title="最终成品门禁修复：补齐敌人和 Boss 场景",
        description="Repair final gate.",
        depth=1,
        status=TaskStatus.WAITING_CHILDREN.value,
        node_meta_json=dumps({"coverage_ids": ["final-product-gate", "brawler-enemy-scenes"]}),
    )
    with SessionLocal() as db:
        db.add(parent)
        db.commit()
        child_ids = orchestrator._insert_child_nodes(
            db,
            parent,
            [
                ChildTaskSpec(
                    title="补齐普通敌人场景与近战 AI",
                    description="Create enemy scenes.",
                    acceptance=["Enemy scenes exist."],
                    coverage_ids=["enemy-scenes"],
                    estimated_leaf=True,
                )
            ],
        )
        db.commit()
        child = db.get(TaskNode, child_ids[0])
        assert child is not None
        meta = loads(child.node_meta_json, {})

    assert meta["final_product_gate_lineage"] is True
    assert meta["final_product_gate_parent_id"] == "parent-final"
    assert "final-product-gate" in meta["coverage_ids"]
    assert orchestrator._is_final_product_gate_task(child) is True


def test_proactive_final_gate_summary_does_not_make_root_a_final_gate_task(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="root-final-summary",
        title="Root project",
        description="Root node with a proactive final gate summary.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
        node_meta_json=dumps({"proactive_final_gate": {"missing_signature": "abc123"}}),
    )
    child = TaskNode(
        id="child-final-marker",
        parent_id=root.id,
        title="Final gate repair",
        description="Repair task.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        node_meta_json=dumps({"proactive_final_gate": True}),
    )

    assert orchestrator._is_final_product_gate_task(root) is False
    assert orchestrator._is_final_product_gate_task(child) is True


def test_final_gate_repair_children_reuse_active_same_signature(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    spec = ChildTaskSpec(
        title="Final gate repair: level scenes",
        description="Repair level scene closure.",
        acceptance=["Level scenes exist."],
        coverage_ids=["final-product-gate", "brawler-level-scenes"],
        verification_kind="deterministic brawler level scene gate",
        estimated_leaf=False,
    )
    signature = orchestrator._child_signature(spec)

    with SessionLocal() as db:
        root = TaskNode(
            id="root-final-dedupe",
            title="Root project",
            description="Root node.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        existing = TaskNode(
            id="existing-final-repair",
            parent_id=root.id,
            title="Final gate repair: level scenes",
            description="Existing active repair.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps(
                {
                    "split_signature": signature,
                    "coverage_ids": ["final-product-gate", "brawler-level-scenes"],
                    "proactive_final_gate": True,
                }
            ),
        )
        db.add_all([root, existing])
        db.commit()
        db.refresh(root)

        child_ids = orchestrator._create_final_gate_repair_children(
            db,
            root,
            {"missing": ["missing level scene"]},
            [spec],
        )
        db.commit()
        children = db.query(TaskNode).filter(TaskNode.parent_id == root.id).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == root.id, EventRecord.type == "final_gate.repair_deduplicated").all()

    assert child_ids == []
    assert [child.id for child in children] == ["existing-final-repair"]
    assert events


def test_final_gate_repair_children_reuse_active_same_category_with_changed_prompt(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    old_spec = ChildTaskSpec(
        title="Final gate repair: level scenes",
        description="Repair old missing level scene list.",
        acceptance=["Old acceptance."],
        coverage_ids=["final-product-gate", "brawler-level-scenes"],
        verification_kind="deterministic brawler level scene gate",
        estimated_leaf=False,
    )
    new_spec = ChildTaskSpec(
        title="Final gate repair: level scenes",
        description="Repair a newer missing level scene list.",
        acceptance=["New acceptance."],
        coverage_ids=["final-product-gate", "brawler-level-scenes"],
        verification_kind="deterministic brawler level scene gate",
        estimated_leaf=False,
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="root-final-category-dedupe",
            title="Root project",
            description="Root node.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        existing = TaskNode(
            id="existing-final-category-repair",
            parent_id=root.id,
            title=old_spec.title,
            description=old_spec.description,
            depth=1,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps(
                {
                    "split_signature": orchestrator._child_signature(old_spec),
                    "coverage_ids": ["final-product-gate", "brawler-level-scenes"],
                    "proactive_final_gate": True,
                }
            ),
        )
        db.add_all([root, existing])
        db.commit()
        db.refresh(root)

        child_ids = orchestrator._create_final_gate_repair_children(
            db,
            root,
            {"missing": ["newer missing level scene"]},
            [new_spec],
        )
        db.commit()
        children = db.query(TaskNode).filter(TaskNode.parent_id == root.id).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == root.id, EventRecord.type == "final_gate.repair_deduplicated").all()

    assert orchestrator._child_signature(old_spec) != orchestrator._child_signature(new_spec)
    assert child_ids == []
    assert [child.id for child in children] == ["existing-final-category-repair"]
    assert events


def test_final_gate_repair_dedupe_uses_db_not_stale_relationship_cache(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    spec = ChildTaskSpec(
        title="Final gate repair: enemy scenes",
        description="Repair enemy scene closure.",
        acceptance=["Enemy scenes exist."],
        coverage_ids=["final-product-gate", "brawler-enemy-scenes"],
        verification_kind="deterministic brawler enemy scene gate",
        estimated_leaf=False,
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="root-final-stale-cache",
            title="Root project",
            description="Root node.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        db.add(root)
        db.commit()
        db.refresh(root)
        assert list(root.children) == []
        existing = TaskNode(
            id="existing-final-cache-repair",
            parent_id=root.id,
            title=spec.title,
            description=spec.description,
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps(
                {
                    "split_signature": orchestrator._child_signature(spec),
                    "coverage_ids": ["final-product-gate", "brawler-enemy-scenes"],
                    "proactive_final_gate": True,
                }
            ),
        )
        db.add(existing)
        db.flush()

        child_ids = orchestrator._create_final_gate_repair_children(
            db,
            root,
            {"missing": ["enemy scene"]},
            [spec],
        )
        db.commit()
        children = db.query(TaskNode).filter(TaskNode.parent_id == root.id).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == root.id, EventRecord.type == "final_gate.repair_deduplicated").all()

    assert child_ids == []
    assert [child.id for child in children] == ["existing-final-cache-repair"]
    assert events


def test_redundant_final_gate_branch_without_active_descendants_is_pruned(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="root-final-branch-prune", title="Root", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        canonical = TaskNode(
            id="canonical-final-branch",
            parent_id=root.id,
            title="最终成品门禁修复：补齐敌人和 Boss 场景",
            description="Canonical branch.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"coverage_ids": ["final-product-gate", "brawler-enemy-scenes"], "split_signature": "same-final-gate"}),
        )
        canonical_active = TaskNode(
            id="canonical-active-child",
            parent_id=canonical.id,
            title="Active child",
            description="Running useful work.",
            depth=2,
            status=TaskStatus.WORKING.value,
        )
        duplicate = TaskNode(
            id="duplicate-final-branch",
            parent_id=root.id,
            title="最终成品门禁修复：补齐敌人和 Boss 场景",
            description="Duplicate branch.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"coverage_ids": ["final-product-gate", "brawler-enemy-scenes"], "split_signature": "same-final-gate"}),
        )
        duplicate_child = TaskNode(
            id="duplicate-queued-child",
            parent_id=duplicate.id,
            title="Queued duplicate child",
            description="Should be superseded.",
            depth=2,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, canonical, canonical_active, duplicate, duplicate_child])
        db.commit()

        children = list(db.query(TaskNode).filter(TaskNode.parent_id == root.id).all())
        settled_ids = orchestrator._settle_redundant_final_gate_sibling_branches(db, root, children)
        db.commit()
        duplicate = db.get(TaskNode, "duplicate-final-branch")
        duplicate_child = db.get(TaskNode, "duplicate-queued-child")
        canonical = db.get(TaskNode, "canonical-final-branch")
        canonical_active = db.get(TaskNode, "canonical-active-child")
        events = db.query(EventRecord).filter(EventRecord.task_id == root.id, EventRecord.type == "final_gate.redundant_branch_pruned").all()

    assert settled_ids == {"duplicate-final-branch", "duplicate-queued-child"}
    assert duplicate is not None and duplicate.status == TaskStatus.DONE.value
    assert duplicate_child is not None and duplicate_child.status == TaskStatus.DONE.value
    assert canonical is not None and canonical.status == TaskStatus.WAITING_CHILDREN.value
    assert canonical_active is not None and canonical_active.status == TaskStatus.WORKING.value
    assert loads(duplicate.result_json, {})["system"]["canonical_task_id"] == "canonical-final-branch"
    assert events


def test_redundant_final_gate_branch_with_active_descendant_is_not_pruned(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="root-final-branch-active", title="Root", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        first = TaskNode(
            id="first-final-branch",
            parent_id=root.id,
            title="最终成品门禁修复：补齐三关战斗场景",
            description="First branch.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"coverage_ids": ["final-product-gate", "brawler-level-scenes"], "split_signature": "same-level-gate"}),
        )
        second = TaskNode(
            id="second-final-branch",
            parent_id=root.id,
            title="最终成品门禁修复：补齐三关战斗场景",
            description="Second branch.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"coverage_ids": ["final-product-gate", "brawler-level-scenes"], "split_signature": "same-level-gate"}),
        )
        second_active = TaskNode(
            id="second-active-child",
            parent_id=second.id,
            title="Active child",
            description="Do not interrupt.",
            depth=2,
            status=TaskStatus.VERIFYING.value,
        )
        db.add_all([root, first, second, second_active])
        db.commit()

        children = list(db.query(TaskNode).filter(TaskNode.parent_id == root.id).all())
        settled_ids = orchestrator._settle_redundant_final_gate_sibling_branches(db, root, children)
        db.commit()
        first = db.get(TaskNode, "first-final-branch")
        second = db.get(TaskNode, "second-final-branch")
        second_active = db.get(TaskNode, "second-active-child")

    assert settled_ids == {"first-final-branch"}
    assert first is not None and first.status == TaskStatus.DONE.value
    assert second is not None and second.status == TaskStatus.WAITING_CHILDREN.value
    assert second_active is not None and second_active.status == TaskStatus.VERIFYING.value


@pytest.mark.asyncio
async def test_passed_root_final_gate_settles_stale_active_repair_branches(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    target = tmp_path / "complete-target"
    (target / "scenes").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="Complete Gate Probe"\nrun/main_scene="res://scenes/main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n[ext_resource type="Script" path="res://scripts/main.gd" id="1"]\n[node name="Main" type="Node2D"]\nscript = ExtResource("1")\n',
        encoding="utf-8",
    )
    (target / "scripts" / "main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (target / "README.md").write_text("Run with Godot using res://scenes/main.tscn.\n", encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(
            id="passed-final-root",
            title="Root",
            description="Build a complete Godot project.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["Final Godot product must be runnable."]),
            constitution_json=dumps({"quality_floor": ["Godot entry must pass."]}),
        )
        stale_branch = TaskNode(
            id="stale-final-branch",
            parent_id=root.id,
            title="Final gate repair: enemy scenes",
            description="Stale repair branch.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"coverage_ids": ["final-product-gate", "brawler-enemy-scenes"], "split_signature": "stale-final"}),
        )
        stale_leaf = TaskNode(
            id="stale-final-leaf",
            parent_id=stale_branch.id,
            title="Stale queued repair leaf",
            description="This should not run once the root gate passes.",
            depth=2,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps({"coverage_ids": ["implementation"]}),
        )
        db.add_all([root, stale_branch, stale_leaf])
        db.commit()

    runner = NoPlanningAfterPassedFinalGateRunner()
    await orchestrator._process_node("passed-final-root", runner, asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, "passed-final-root")
        stale_branch = db.get(TaskNode, "stale-final-branch")
        stale_leaf = db.get(TaskNode, "stale-final-leaf")
        events = db.query(EventRecord).filter(EventRecord.task_id == "passed-final-root", EventRecord.type == "final_gate.passed_branches_settled").all()

    assert root is not None and root.status == TaskStatus.DONE.value
    assert stale_branch is not None and stale_branch.status == TaskStatus.DONE.value
    assert stale_leaf is not None and stale_leaf.status == TaskStatus.DONE.value
    assert runner.accepted_task_ids == ["passed-final-root"]
    assert loads(root.result_json, {})["final_gate"]["passed"] is True
    assert loads(stale_branch.result_json, {})["system"]["status"] == "superseded_by_passed_root_final_gate"
    assert loads(stale_leaf.node_meta_json, {})["superseded_by_passed_root_final_gate"] == "passed-final-root"
    assert events


@pytest.mark.asyncio
async def test_idle_api_restart_flag_does_not_drain_new_agent_starts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "restart_api_when_idle.flag").write_text("load latest code when idle\n", encoding="ascii")
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=workspace,
        runner_mode="real",
        goal_mode=True,
        agent_burst_limit=1,
    )
    orchestrator = Orchestrator(settings)

    root_id = "idle-restart-root"
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id=root_id,
                title="Root",
                description="Keep fueling while API waits to restart.",
                depth=0,
                status=TaskStatus.PLANNING.value,
                node_meta_json=dumps({"runner_mode": "real"}),
            )
        )
        db.commit()

    reserved = await orchestrator._reserve_agent_start(root_id, "planner")
    orchestrator._release_pending_codex_slot(root_id)

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        events = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "scheduler.api_restart_drain").all()

    assert reserved is True
    assert root.status == TaskStatus.PLANNING.value
    assert "api_restart_drain" not in meta
    assert not events


@pytest.mark.asyncio
async def test_api_restart_drain_flag_drains_new_agent_starts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "restart_api_when_idle.flag").write_text("load latest code when idle\n", encoding="ascii")
    (workspace / "restart_api_drain_when_safe.flag").write_text("allow short hibernation for maintenance restart\n", encoding="ascii")
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=workspace,
        runner_mode="real",
        goal_mode=True,
        agent_burst_limit=1,
    )
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test API restart drain.")
    root_id = await orchestrator.start_project("Test API restart drain.", constitution_id=constitution_id, auto_run=False)
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.status = TaskStatus.PLANNING.value
        db.commit()

    reserved = await orchestrator._reserve_agent_start(root_id, "planner")

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        events = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "scheduler.api_restart_drain").all()

    assert reserved is False
    assert root.status == TaskStatus.QUEUED.value
    assert meta["api_restart_drain"]["phase"] == "planner"
    assert orchestrator._agent_starts_by_root[root_id] == 0
    assert events


def test_watchdog_hook_state_goal_mode_disables_burst_budget():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        goal_mode=True,
        agent_burst_limit=8,
        agent_burst_reserve=1,
    )
    orchestrator = Orchestrator(settings)

    state = orchestrator._watchdog_hook_state("root-goal-mode")

    assert "-GoalMode" in state["watchdog_run_command"]
    assert "-GoalMode" in state["watchdog_install_command"]
    assert "-AgentBurstLimit 0" in state["watchdog_run_command"]
    assert "-AgentBurstReserve 0" in state["watchdog_run_command"]
    assert "Goal 长跑模式已开启" in state["token_reserve_summary"]


def test_godot_headless_crash_blocks_as_validation_environment():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    orchestrator = Orchestrator(settings)
    task = TaskNode(
        id="godot-crash-node",
        title="Godot crash node",
        description="Validate Godot startup.",
        depth=1,
        status=TaskStatus.BLOCKED.value,
        scope_json=dumps([]),
        acceptance_json=dumps([]),
    )

    report = orchestrator._build_block_report(
        task,
        "Godot_v4.6.3-stable_win64.exe 应用程序错误：该内存不能 read。"
        " ERROR: Could not create directory: 'user://logs'. CrashHandlerException: Program crashed with signal 11.",
        "worker.blocked",
        None,
    )

    assert report["category"] == "validation_environment"
    assert report["retryable"] is False
    assert report["parent_action"] == "recursive_recovery_static_wrapper"


def test_unknown_external_block_recommends_skill_lookup_recovery():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        skill_lookup_enabled=True,
        skill_km_url="https://skill-library.example/skill?skillId=test",
    )
    orchestrator = Orchestrator(settings)
    task = TaskNode(
        id="unknown-block-node",
        title="Unknown block node",
        description="Recover from unknown issue.",
        depth=1,
        status=TaskStatus.BLOCKED.value,
        scope_json=dumps([]),
        acceptance_json=dumps([]),
    )

    report = orchestrator._build_block_report(task, "未知第三方工具报错，需要查资料。", "worker.blocked", None)

    assert report["category"] == "external"
    assert report["retryable"] is False
    assert report["parent_action"] == "skill_lookup_recovery"
    assert report["skill_lookup"]["url"].endswith("skillId=test")
    assert report["skill_lookup"]["recommended"] is True


def test_login_or_permission_block_is_human_auth_required():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        skill_lookup_enabled=True,
        skill_km_url="https://skill-library.example/skill?skillId=test",
    )
    orchestrator = Orchestrator(settings)
    task = TaskNode(
        id="login-block-node",
        title="Login block node",
        description="Needs KM lookup.",
        depth=1,
        status=TaskStatus.BLOCKED.value,
        scope_json=dumps([]),
        acceptance_json=dumps([]),
    )

    report = orchestrator._build_block_report(task, "Workhub Skill/KM 需要登录 SSO，返回 403 权限不足。", "worker.blocked", None)

    assert report["category"] == "human_auth"
    assert report["retryable"] is False
    assert report["parent_action"] == "human_auth_required"
    assert report["human_action_required"] is True


def test_visual_uncertain_block_requires_human_visual_review():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    orchestrator = Orchestrator(settings)
    task = TaskNode(
        id="visual-uncertain-node",
        title="Visual uncertain node",
        description="Judge attack VFX polish.",
        depth=1,
        status=TaskStatus.BLOCKED.value,
        scope_json=dumps([]),
        acceptance_json=dumps([]),
    )

    report = orchestrator._build_block_report(task, "视觉不确定：无法可靠判断打击感是否达到街机商业品质。", "verifier.visual_uncertain", None)

    assert report["category"] == "visual_uncertain"
    assert report["retryable"] is False
    assert report["parent_action"] == "human_visual_review"
    assert report["human_action_required"] is True


def test_serialize_event_falls_back_for_unreadable_historical_message():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    event = EventRecord(
        task_id="root-unreadable-event",
        type="tree.reconstructed",
        message="??????????????????????????????",
        payload_json=dumps({"target_path": r"C:\projects\demo-target"}),
    )

    payload = orchestrator.serialize_event(event)

    assert payload["message"] == r"任务树已从本地状态快照重建；目标目录：C:\projects\demo-target。"
    assert "????" not in payload["message"]
    assert payload["payload"]["target_path"] == r"C:\projects\demo-target"


@pytest.mark.asyncio
async def test_watchdog_state_marks_due_hibernation_for_auto_resume():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        agent_burst_limit=8,
        agent_burst_reserve=1,
        machine_safety_min_available_memory_mb=0,
    )
    orchestrator = Orchestrator(settings)
    orchestrator._codex_slots_active = lambda *args, **kwargs: 0
    constitution_id = await locked_constitution(orchestrator, "Build a watchdog wakeup probe.")
    root_id = await orchestrator.start_project(
        goal="Build a watchdog wakeup probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )

    await orchestrator.hibernate_tree(root_id, 3600, "wait for token refresh")
    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        meta["hibernation"]["resume_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        root.node_meta_json = dumps(meta)
        db.commit()

        state = orchestrator.get_watchdog_state(db)

    assert state["root_id"] == root_id
    assert state["selection"] == "auto_latest"
    assert state["resume_due"] is True
    assert state["should_resume"] is True
    assert state["recommended_action"] == "resume"
    assert state["agent_burst_limit"] == 8
    assert state["agent_burst_reserve"] == 1
    assert state["agent_burst_work_limit"] == 7
    assert state["budget_warning_level"] == "paused"
    assert "自动续跑" in state["budget_warning_message"]


@pytest.mark.asyncio
async def test_watchdog_state_resumes_resource_hibernation_when_retry_after_elapsed(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=workspace,
        goal_mode=True,
        machine_safety_min_available_memory_mb=0,
    )
    orchestrator = Orchestrator(settings)
    orchestrator._codex_slots_active = lambda *args, **kwargs: 0
    constitution_id = await locked_constitution(orchestrator, "Build a quota wakeup probe.")
    root_id = await orchestrator.start_project(
        goal="Build a quota wakeup probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )

    await orchestrator.hibernate_tree(root_id, 3600, "Detected retryable Codex resource/quota block.")
    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)
    (workspace / "codex_health.json").write_text(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "resource_block",
                "summary": "codex_runtime_retry_after_observed",
                "quota_observability": "retry_after_observed_from_codex_output",
                "quota_retry_after_seconds": 3600,
                "quota_reset_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                "quota_retry_after_source": "codex_output",
                "quota_signal": "retry-after: 3600",
                "quota_source": "run_log:runs/node-a/worker.jsonl",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with SessionLocal() as db:
        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    assert state["resume_due"] is True
    assert state["should_resume"] is True
    assert state["recommended_action"] == "resume"
    assert state["codex_health_status"] == "warning"
    assert state["codex_quota_observability"] == "retry_after_elapsed_from_codex_output"
    assert "Codex resource hibernation recovered" in state["reason"]


@pytest.mark.asyncio
async def test_budget_guard_blocks_due_hibernation_auto_resume(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        agent_burst_limit=0,
        goal_mode=True,
        machine_safety_min_available_memory_mb=0,
    )
    orchestrator = Orchestrator(settings)
    constitution_id = await locked_constitution(orchestrator, "Build a budget guarded wakeup probe.")
    root_id = await orchestrator.start_project(
        goal="Build a budget guarded wakeup probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )

    await orchestrator.hibernate_tree(root_id, 60, "wait for account quota refresh")
    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)
    await orchestrator.set_budget_guard(root_id, 3600, "余额不足，等待 5 小时额度刷新。", hibernate_tree=False)

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        meta["hibernation"]["resume_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        root.node_meta_json = dumps(meta)
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    assert state["budget_guard_active"] is True
    assert state["resume_due"] is False
    assert state["should_resume"] is False
    assert state["recommended_action"] == "budget_guard_sleep"
    assert state["budget_warning_level"] == "sleeping"
    assert "预算守卫" in state["budget_warning_message"]

    resume = await orchestrator.resume_hibernated_tree(root_id)

    assert resume["status"] == "budget_guard_sleep"
    assert resume["count"] == 0
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        assert root.status == TaskStatus.HIBERNATING.value
    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)


def test_retry_after_report_controls_resource_hibernate_window(tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace", resource_block_resume_after_seconds=600)
    orchestrator = Orchestrator(settings)

    assert (
        orchestrator._retry_after_seconds_from_reports(
            [
                {
                    "category": "rate_limit",
                    "retryable": True,
                    "retry_after_seconds": 18_000,
                    "reason": "429 rate limit, retry after 5 hours",
                }
            ]
        )
        == 18_000
    )
    assert orchestrator._retry_after_seconds_from_reports([]) == 600


@pytest.mark.asyncio
async def test_watchdog_state_marks_orphaned_active_tree_for_auto_resume():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        watchdog_orphan_resume_after_seconds=60,
    )
    orchestrator = Orchestrator(settings)
    orchestrator._codex_slots_active = lambda *args, **kwargs: 0
    constitution_id = await locked_constitution(orchestrator, "Build an orphaned scheduler probe.")
    root_id = await orchestrator.start_project(
        goal="Build an orphaned scheduler probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.status = TaskStatus.QUEUED.value
        root.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    assert state["root_id"] == root_id
    assert state["active_agents"] == 0
    assert state["active_frontier"] == 1
    assert state["orphaned_active_tree"] is True
    assert state["should_resume"] is True
    assert state["recommended_action"] == "resume_orphaned"
    assert state["last_activity_age_seconds"] >= 60


@pytest.mark.asyncio
async def test_watchdog_state_does_not_invent_reason_when_tree_is_not_hibernating():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        agent_burst_limit=8,
        agent_burst_reserve=1,
        watchdog_orphan_resume_after_seconds=3600,
    )
    orchestrator = Orchestrator(settings)
    orchestrator._codex_slots_active = lambda *args, **kwargs: 0
    constitution_id = await locked_constitution(orchestrator, "Build a normal watchdog status probe.")
    root_id = await orchestrator.start_project(
        goal="Build a normal watchdog status probe.",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.status = TaskStatus.QUEUED.value
        root.updated_at = datetime.now(timezone.utc)
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    assert state["hibernating"] == 0
    assert state["resume_due"] is False
    assert state["recommended_action"] == "top_off"
    assert state["reason"] == ""


@pytest.mark.asyncio
async def test_one_shot_goal_only_mock_run_converges_to_final_godot_product(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        runner_mode="mock",
        workspace_dir=tmp_path / "workspace",
        max_concurrency=1,
    )
    orchestrator = Orchestrator(settings)

    root_id = await orchestrator.start_one_shot_project(ARCADE_BRAWLER_GOAL)
    await drain_background_tasks(orchestrator)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id)
        root = next(task for task in tree["tasks"] if task["id"] == root_id)
        constitution = tree["constitution"]
        run_kinds = {run.kind for run in db.query(RunRecord).all()}

    assert root["description"] == ARCADE_BRAWLER_GOAL
    assert root["status"] == TaskStatus.DONE.value
    assert root["node_meta"]["runner_mode"] == "mock"
    assert tree["stats"]["total"] > 1
    assert tree["stats"]["done"] == tree["stats"]["total"]
    assert tree["stats"]["blocked"] == 0
    assert tree["stats"]["failed"] == 0
    assert tree["stats"]["hibernating"] == 0
    assert tree["stats"]["max_concurrency"] == 1

    assert constitution is not None
    assert constitution["goal"] == ARCADE_BRAWLER_GOAL
    assert constitution["status"] == "locked"
    assert constitution["root_id"] == root_id
    assert constitution["target_path"]
    target = Path(constitution["target_path"])
    target.resolve().relative_to((tmp_path / "workspace" / "projects").resolve())
    assert target.name.startswith("godot-")

    assert (target / "project.godot").is_file()
    assert (target / "README.md").is_file()
    assert (target / "scenes" / "main.tscn").is_file()
    assert (target / "scripts" / "game_controller.gd").is_file()
    assert (target / "data" / "characters" / "player_hero.json").is_file()
    assert (target / "data" / "enemies" / "enemy_catalog.json").is_file()
    assert (target / "data" / "levels" / "level_catalog.json").is_file()
    assert (target / "data" / "spawns" / "arcade_wave_plan.json").is_file()
    assert len(list((target / "assets").rglob("*.png"))) >= 8
    assert len(list((target / "assets").rglob("*.wav"))) >= 2

    final_gate = root["result"]["final_gate"]
    assert final_gate["required"] is True
    assert final_gate["passed"] is True
    assert final_gate["missing"] == []
    assert all(check["passed"] for check in final_gate["checks"])
    passed_names = {check["name"] for check in final_gate["checks"] if check["passed"]}
    assert "brawler_visual_asset_set" in passed_names
    assert "brawler_visual_assets_referenced" in passed_names
    assert "brawler_audio_asset_set" in passed_names
    assert "brawler_audio_assets_referenced" in passed_names
    assert {
        RunKind.PLANNER.value,
        RunKind.WORKER.value,
        RunKind.VERIFIER.value,
        RunKind.ACCEPTANCE.value,
    }.issubset(run_kinds)


class AcceptanceAlwaysRepairRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            payload = PlannerDecision(
                decision="decompose",
                reason="Root must split into children.",
                children=[
                    ChildTaskSpec(title="Child A", description="Do A.", acceptance=["A passes."]),
                    ChildTaskSpec(title="Child B", description="Do B.", acceptance=["B passes."]),
                ],
            ).model_dump()
        else:
            payload = PlannerDecision(decision="execute", reason="Leaf is small enough.", leaf_certainty="300%").model_dump()
        return envelope("planner", payload)

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary=f"{task.title} done.",
                changed_files=[f"{task.id}.txt"],
                evidence=[f"{task.title} evidence"],
            ).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Leaf verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope(
            "acceptance",
            AcceptanceResult(
                verdict="repair",
                reason="Parent integration still needs repair.",
                suggested_children=[
                    ChildTaskSpec(title="Integration repair", description="Repair integration.", acceptance=["Repair evidence exists."]),
                ],
                constitution_verdict="within_bounds",
            ).model_dump(),
        )


class SlowPlanningRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.plan_calls = 0

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        self.plan_calls += 1
        await asyncio.sleep(0.05)
        return envelope("planner", PlannerDecision(decision="execute", reason="Execute once.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "worker",
            WorkerResult(status="succeeded", summary="Done.", changed_files=[f"{task.id}.txt"], evidence=["evidence"]).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


class VisualUncertainVerifierRunner(SlowPlanningRunner):
    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope(
            "verifier",
            VerifierResult(
                verdict="visual_uncertain",
                reason="视觉不确定：已有截图证据，但无法可靠判断动作打击感是否达到街机商业品质。",
                checks=["截图存在。", "动作帧存在。", "主观打击感需要人工判断。"],
                evidence=["visual/review/attack_hit.png"],
                constitution_verdict="within_bounds",
            ).model_dump(),
        )


class VisualUncertainWithEvidenceChildrenRunner(SlowPlanningRunner):
    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        if task.depth == 0:
            return envelope(
                "verifier",
                VerifierResult(
                    verdict="visual_uncertain",
                    reason="视觉证据不足：需要补一组攻击命中帧序列后才能继续判断。",
                    checks=["缺少攻击命中帧序列。"],
                    evidence=["visual/review/attack_hit.png"],
                    suggested_children=[
                        ChildTaskSpec(
                            title="补齐攻击命中视觉帧证据",
                            description="生成或采集攻击命中前中后三帧证据，并给出可复查路径。",
                            acceptance=["必须产出可被父节点复查的帧序列或视觉证据报告。"],
                            verification_kind="visual evidence capture",
                            estimated_leaf=True,
                        )
                    ],
                    leaf_misclassified=True,
                    constitution_verdict="within_bounds",
                    failure_to_decomposition_reason="视觉证据不足，需要先递归补证。",
                ).model_dump(),
            )
        return await super().verify(task, worker_result)


class AcceptanceVisualUncertainWithEvidenceChildrenRunner(SlowPlanningRunner):
    def __init__(self) -> None:
        super().__init__()
        self.accept_calls = 0

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        self.accept_calls += 1
        if self.accept_calls == 1:
            return envelope(
                "acceptance",
                AcceptanceResult(
                    verdict="visual_uncertain",
                    reason="父节点视觉整合证据不足：需要补一份可复查的战斗画面证据报告。",
                    checks=["已有子成果，但缺少整合后的战斗画面证据。"],
                    evidence=[],
                    suggested_children=[
                        ChildTaskSpec(
                            title="补齐整合战斗画面证据",
                            description="在父节点集成结果中补齐战斗画面截图、帧序列或代理指标报告。",
                            acceptance=["必须证明父节点集成结果具备可见玩家、敌人、HUD 和命中特效证据。"],
                            verification_kind="integrated visual evidence",
                            estimated_leaf=True,
                        )
                    ],
                    constitution_verdict="within_bounds",
                ).model_dump(),
            )
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="视觉补证后父节点验收通过。").model_dump())


class PlannerRepairRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            return envelope(
                "planner",
                PlannerDecision(
                    decision="repair",
                    reason="Planner thinks this parent needs repair before execution.",
                    children=[
                        ChildTaskSpec(
                            title="Planner repair child",
                            description="Do the repair recursively.",
                            acceptance=["Repair evidence exists."],
                            estimated_leaf=True,
                        )
                    ],
                ).model_dump(),
            )
        return envelope("planner", PlannerDecision(decision="execute", reason="Repair child is executable.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary=f"{task.title} repaired.",
                changed_files=["repair.txt"],
                evidence=["repair evidence"],
            ).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Repair verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Parent accepted repaired child.").model_dump())


class PlannerBlockedRunner(BaseCodexRunner):
    reason = "Planner cannot find a safe execution boundary yet."

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            return envelope(
                "planner",
                PlannerDecision(
                    decision="blocked",
                    reason=self.reason,
                    blocker="boundary_unclear",
                ).model_dump(),
            )
        return envelope("planner", PlannerDecision(decision="execute", reason="Recovery child is executable.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary=f"{task.title} recovered.",
                changed_files=["planner-recovery.txt"],
                evidence=["planner blocked recovery evidence"],
            ).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Recovery verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Parent accepted planner recovery child.").model_dump())


class ToolInternalPlannerBlockedRunner(PlannerBlockedRunner):
    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        payload = PlannerDecision(
            decision="blocked",
            reason="工具内部错误，已阻止递归拆分并等待工具修复：Codex planner exited with code 2. unexpected argument",
        ).model_dump()
        payload["block_report"] = {
            "category": "tool_internal",
            "retryable": False,
            "source": "planner",
            "reason": payload["reason"],
            "attempts": 1,
            "parent_action": "pause_tool_fix",
        }
        return envelope("planner", payload)


class EnvironmentBlockedPlannerRunner(PlannerBlockedRunner):
    """实测形态（2026-09-22）：模型如实报告"只读命令在进程创建时被执行策略拒绝"。

    这条阻塞既不是工具内部错误、也不是资源类可等待阻塞——它是**环境性**的，递归解决不了。
    真跑时系统对它每层都转子任务，最后跑成螺旋并把跑批拖到冻住。
    """

    reason = (
        "已确认真实外部阻塞：在目标目录执行只读命令 `rg -n . calc/ops.py tests/test_ops.py`，"
        "进程创建被执行策略以 `blocked by policy` 拒绝，命令未运行。"
    )

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth <= 1:
            return envelope("planner", PlannerDecision(decision="blocked", reason=self.reason).model_dump())
        return envelope("planner", PlannerDecision(decision="execute", reason="Recovery child is executable.", leaf_certainty="300%").model_dump())


class EnvironmentDeclaredPlannerRunner(PlannerBlockedRunner):
    """按新合约声明"环境阻塞"：`block_report.category=environment`（见 `prompts/planner.md`）。"""

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        reason = "在目标目录执行只读命令时进程创建被执行策略以 `blocked by policy` 拒绝，命令未运行。"
        payload = PlannerDecision(decision="blocked", reason=reason).model_dump()
        payload["block_report"] = {
            "category": "environment",
            "reason": reason,
            "retryable": False,
            "parent_action": "human_review",
        }
        return envelope("planner", payload)


class RateLimitPlannerBlockedRunner(PlannerBlockedRunner):
    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "planner",
            PlannerDecision(decision="blocked", reason="Planner cannot proceed: 429 rate limit, retry after 5 hours.").model_dump(),
        )


class AcceptanceBlockedOnceRunner(BaseCodexRunner):
    reason = "Integrated child outputs are present, but the parent cannot verify a runnable entry yet."

    def __init__(self) -> None:
        self.accept_calls: dict[str, int] = {}

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            return envelope(
                "planner",
                PlannerDecision(
                    decision="decompose",
                    reason="Root needs child outputs before acceptance.",
                    children=[
                        ChildTaskSpec(title="Child A", description="Build A.", acceptance=["A evidence exists."]),
                        ChildTaskSpec(title="Child B", description="Build B.", acceptance=["B evidence exists."]),
                    ],
                ).model_dump(),
            )
        return envelope("planner", PlannerDecision(decision="execute", reason="Child is executable.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary=f"{task.title} done.",
                changed_files=[f"{task.id}.txt"],
                evidence=[f"{task.title} evidence"],
            ).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        calls = self.accept_calls.get(task.id, 0) + 1
        self.accept_calls[task.id] = calls
        if task.depth == 0 and calls == 1:
            return envelope(
                "acceptance",
                AcceptanceResult(
                    verdict="blocked",
                    reason=self.reason,
                    constitution_verdict="within_bounds",
                ).model_dump(),
            )
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Parent accepted recovered integration.").model_dump())


class AcceptanceAlwaysBlockedRunner(AcceptanceBlockedOnceRunner):
    """任何深度都 blocked：给"恢复链上的父节点再被 blocked"这条用例用。"""

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        self.accept_calls[task.id] = self.accept_calls.get(task.id, 0) + 1
        return envelope(
            "acceptance",
            AcceptanceResult(
                verdict="blocked",
                reason=self.reason,
                constitution_verdict="within_bounds",
            ).model_dump(),
        )


class FinalGateRepairRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        return envelope("planner", PlannerDecision(decision="execute", reason="Final gate repair is bounded.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        worktree = Path(task.worktree_path or ".")
        (worktree / "scenes" / "main").mkdir(parents=True, exist_ok=True)
        (worktree / "scripts").mkdir(parents=True, exist_ok=True)
        (worktree / "project.godot").write_text(
            'config_version=5\n\n[application]\nconfig/name="Final Gate Brawler"\nrun/main_scene="res://scenes/main/Main.tscn"\n',
            encoding="utf-8",
        )
        (worktree / "scenes" / "main" / "Main.tscn").write_text(
            '[gd_scene load_steps=2 format=3]\n[ext_resource type="Script" path="res://scripts/player.gd" id="1"]\n[node name="Main" type="Node2D"]\nscript = ExtResource("1")\n',
            encoding="utf-8",
        )
        (worktree / "scripts" / "player.gd").write_text("extends Node2D\n", encoding="utf-8")
        (worktree / "README.md").write_text("启动入口：scenes/main/Main.tscn\n验证：Godot 静态项目检查。\n", encoding="utf-8")
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary="Final gate files created.",
                changed_files=["project.godot", "scenes/main/Main.tscn", "scripts/player.gd", "README.md"],
                evidence=["project.godot has run/main_scene", "main scene and script exist"],
            ).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Final gate repair verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


class NoRootPlannerFinalGateRepairRunner(FinalGateRepairRunner):
    def __init__(self) -> None:
        self.planned_task_ids: list[str] = []

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        self.planned_task_ids.append(task.id)
        if task.parent_id is None:
            raise AssertionError("root planner should not run before deterministic final gate repair")
        return await super().plan(task)


class CountingAcceptanceFinalGateRepairRunner(FinalGateRepairRunner):
    def __init__(self) -> None:
        self.accepted_task_ids: list[str] = []

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        self.accepted_task_ids.append(task.id)
        return await super().accept(task, children)


class NoPlanningAfterPassedFinalGateRunner(FinalGateRepairRunner):
    def __init__(self) -> None:
        self.accepted_task_ids: list[str] = []

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        raise AssertionError("stale final-gate repair branches should be settled before planner runs")

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        raise AssertionError("stale final-gate repair branches should be settled before worker runs")

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        raise AssertionError("stale final-gate repair branches should be settled before verifier runs")

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        self.accepted_task_ids.append(task.id)
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Already converged.").model_dump())


class IntegrationGateRepairRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        return envelope("planner", PlannerDecision(decision="execute", reason="Integration repair is bounded.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        worktree = Path(task.worktree_path or ".")
        (worktree / "scenes" / "levels").mkdir(parents=True, exist_ok=True)
        (worktree / "scenes" / "levels" / "MissingDock.tscn").write_text(
            '[gd_scene load_steps=2 format=3]\n[node name="MissingDock" type="Node2D"]\n[node name="PlayerSpawn" type="Marker2D" parent="."]\n',
            encoding="utf-8",
        )
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary="Missing integration scene created.",
                changed_files=["scenes/levels/MissingDock.tscn"],
                evidence=["missing res:// scene now exists"],
            ).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Integration repair verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


@pytest.mark.asyncio
async def test_planner_repair_is_converted_to_recursive_children():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="planner-repair-root", title="Root", description="Parent needs planner repair.", depth=0, status=TaskStatus.PLANNED.value)
        db.add(root)
        db.commit()

    await orchestrator._process_node("planner-repair-root", PlannerRepairRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, "planner-repair-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "planner-repair-root").all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "planner-repair-root").all()

    assert root is not None and root.status == TaskStatus.DONE.value
    assert len(children) == 1
    assert children[0].title == "Planner repair child"
    assert children[0].status == TaskStatus.DONE.value
    assert any(event.type == "planner.repair_converted" for event in events)


@pytest.mark.asyncio
async def test_planner_blocked_with_objective_is_converted_to_recursive_recovery():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(
            id="planner-blocked-root",
            title="Root",
            description="Build a concrete feature, but planner initially cannot find the boundary.",
            depth=0,
            status=TaskStatus.PLANNED.value,
        )
        db.add(root)
        db.commit()

    await orchestrator._process_node("planner-blocked-root", PlannerBlockedRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, "planner-blocked-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "planner-blocked-root").all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "planner-blocked-root").all()
        meta = loads(root.node_meta_json, {}) if root else {}

    assert root is not None and root.status == TaskStatus.DONE.value
    assert len(children) == 1
    assert "规划阻塞恢复" in children[0].title
    assert children[0].status == TaskStatus.DONE.value
    assert meta["planner_blocked_recovery_signatures"]
    assert any(event.type == "planner.blocked_converted" for event in events)


@pytest.mark.asyncio
async def test_tool_internal_planner_blocked_is_not_converted_to_recursive_recovery():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    root = TaskNode(
        id="planner-tool-internal-root",
        title="Tool internal planner blocked root",
        description="This objective is valid, but the tool invocation failed.",
        depth=0,
        status=TaskStatus.QUEUED.value,
        scope_json=dumps([]),
        acceptance_json=dumps(["Objective remains valid."]),
        constitution_json=dumps({}),
    )
    with SessionLocal() as db:
        db.add(root)
        db.commit()

    await orchestrator._process_node("planner-tool-internal-root", ToolInternalPlannerBlockedRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, "planner-tool-internal-root")
        assert root is not None
        children = db.query(TaskNode).filter(TaskNode.parent_id == "planner-tool-internal-root").all()
        meta = loads(root.node_meta_json, {})
        events = db.query(EventRecord).filter(EventRecord.task_id == "planner-tool-internal-root").all()

    assert root.status == TaskStatus.BLOCKED.value
    assert children == []
    assert meta["block_report"]["category"] == "tool_internal"
    assert meta["block_report"]["parent_action"] == "pause_tool_fix"
    assert not any(event.type == "planner.blocked_converted" for event in events)


@pytest.mark.asyncio
async def test_repeated_planner_blocked_recovery_signature_blocks_for_safety():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="planner-blocked-repeat-root",
        title="Root",
        description="Build a concrete feature, but planner repeatedly blocks.",
        depth=0,
        status=TaskStatus.PLANNED.value,
    )
    signature = orchestrator._planner_blocked_recovery_signature(root, PlannerBlockedRunner.reason)
    root.node_meta_json = dumps({"planner_blocked_recovery_signatures": [signature]})
    with SessionLocal() as db:
        db.add(root)
        db.commit()

    await orchestrator._process_node("planner-blocked-repeat-root", PlannerBlockedRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, "planner-blocked-repeat-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "planner-blocked-repeat-root").all()
        meta = loads(root.node_meta_json, {}) if root else {}

    assert root is not None and root.status == TaskStatus.BLOCKED.value
    assert children == []
    assert meta["block_report"]["source"] == "planner.blocked"
    assert meta["planner_blocked_recovery_repeated"]["signature"] == signature


@pytest.mark.asyncio
async def test_acceptance_blocked_is_converted_to_recursive_recovery_child():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(
            id="acceptance-blocked-root",
            title="Root",
            description="Integrate concrete child outputs into a runnable parent.",
            depth=0,
            status=TaskStatus.PLANNED.value,
        )
        db.add(root)
        db.commit()

    await orchestrator._process_node("acceptance-blocked-root", AcceptanceBlockedOnceRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, "acceptance-blocked-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "acceptance-blocked-root").order_by(TaskNode.created_at).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "acceptance-blocked-root").all()
        meta = loads(root.node_meta_json, {}) if root else {}

    assert root is not None and root.status == TaskStatus.DONE.value
    assert len(children) == 3
    assert any("整合阻塞恢复" in child.title for child in children)
    assert all(child.status == TaskStatus.DONE.value for child in children)
    assert meta["acceptance_blocked_recovery_signatures"]
    assert any(event.type == "acceptance.blocked_converted" for event in events)


@pytest.mark.asyncio
async def test_repeated_acceptance_blocked_recovery_signature_blocks_for_safety():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    runner = AcceptanceBlockedOnceRunner()
    root = TaskNode(
        id="acceptance-blocked-repeat-root",
        title="Root",
        description="Integrate concrete child outputs, but acceptance repeatedly blocks.",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
    )
    signature = orchestrator._acceptance_blocked_recovery_signature(root, AcceptanceBlockedOnceRunner.reason)
    root.node_meta_json = dumps({"acceptance_blocked_recovery_signatures": [signature]})
    child = TaskNode(
        id="acceptance-repeat-child",
        parent_id=root.id,
        title="Existing child",
        description="Already done child output.",
        depth=1,
        status=TaskStatus.DONE.value,
        result_json=dumps(
            {
                "worker": WorkerResult(
                    status="succeeded",
                    summary="Done.",
                    changed_files=["existing.txt"],
                    evidence=["existing evidence"],
                ).model_dump(),
                "verifier": VerifierResult(verdict="pass", reason="Verified.").model_dump(),
            }
        ),
    )
    with SessionLocal() as db:
        db.add_all([root, child])
        db.commit()

    await orchestrator._accept_parent("acceptance-blocked-repeat-root", runner, orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, "acceptance-blocked-repeat-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "acceptance-blocked-repeat-root").all()
        meta = loads(root.node_meta_json, {}) if root else {}

    assert root is not None and root.status == TaskStatus.BLOCKED.value
    assert len(children) == 1
    assert meta["block_report"]["source"] == "acceptance.blocked"
    assert meta["acceptance_blocked_recovery_repeated"]["signature"] == signature


@pytest.mark.asyncio
async def test_environmental_planner_blocked_stops_after_one_recovery_level():
    """环境性阻塞：**允许一层恢复，第二层必须停在上报**（防"规划阻塞恢复"螺旋）。

    实测（2026-09-22）：模型报告"只读命令在进程创建时被执行策略以 `blocked by policy` 拒绝"，
    系统每一层都把 blocked 转成"规划阻塞恢复"子任务 → 3-5 分钟到深度 4、并发油门 1→3、
    标题叠成"…：规划阻塞恢复：规划阻塞恢复：…"，最后把整批跑批拖到冻住（30-60 分钟不醒）。

    失效方向写明：环境问题递归解决不了 → **宁可把树标成 blocked**（人可事后查，事件与 meta 里写明
    抑制原因），也不要假装在推进。反面自检：第一层**仍然**要转（否则就是"静默死停"，是另一种坏）。
    """
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    with SessionLocal() as db:
        db.add(TaskNode(id="env-blocked-root", title="Root", description="让失败的测试通过。", depth=0, status=TaskStatus.PLANNED.value))
        db.commit()

    await orchestrator._process_node("env-blocked-root", EnvironmentBlockedPlannerRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        level1 = db.query(TaskNode).filter(TaskNode.parent_id == "env-blocked-root").all()
        # 按**结构标记**挑恢复子节点：并行的"修复或替代阻塞子成果：…规划阻塞恢复"是另一条机制
        # （标题里也带这几个字），用标题匹配会把两条机制混在一起。
        recovery = [child for child in level1 if any(str(item).startswith("planner-blocked:") for item in loads(child.scope_json, []))]
        level2 = db.query(TaskNode).filter(TaskNode.parent_id == recovery[0].id).all() if recovery else []
        root_events = db.query(EventRecord).filter(EventRecord.task_id == "env-blocked-root").all()
        recovery_events = db.query(EventRecord).filter(EventRecord.task_id == recovery[0].id).all() if recovery else []
        recovery_meta = loads(recovery[0].node_meta_json, {}) if recovery else {}

    assert recovery, "第一层仍然要转成恢复子任务（父层不静默死停）"
    assert len(recovery) == 1, [(c.id, c.title, c.status, c.parent_id) for c in level1]
    assert any(event.type == "planner.blocked_converted" for event in root_events)

    assert level2 == [], "第二层必须停：环境性阻塞递归解决不了"
    assert recovery[0].status == TaskStatus.BLOCKED.value
    assert "阻塞恢复链" in recovery_meta["planner_blocked_recovery_suppressed"]
    assert recovery_meta["block_report"]["source"] == "planner.blocked"
    assert not any(event.type == "planner.blocked_converted" for event in recovery_events)
    # 恢复链标记本身要落在 scope 里（判据读的就是它，别让"标记没写进去"把这条测试变成恒真）
    assert any(str(item).startswith("planner-blocked:") for item in loads(recovery[0].scope_json, []))


@pytest.mark.asyncio
async def test_declared_environment_block_is_not_converted_to_recovery_children():
    """planner **显式声明**环境阻塞 → 一次都不拆，直接停下来报给人（BC-4 的结构化那条）。

    与"恢复链只允许一层"是两层判据：这一层是**识别**（模型说了原因），那一层是兜底
    （模型没说时也不会无限递归）。环境阻塞递归解决不了——实测那种螺旋会把跑批拖垮。
    """
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="env-declared-root",
                title="Root",
                description="让失败的测试通过（不许改任何文件）。",
                depth=0,
                status=TaskStatus.PLANNED.value,
            )
        )
        db.commit()

    await orchestrator._process_node("env-declared-root", EnvironmentDeclaredPlannerRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, "env-declared-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "env-declared-root").all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "env-declared-root").all()
        meta = loads(root.node_meta_json, {}) if root else {}

    assert children == [], "声明为环境阻塞就不该拆恢复子任务"
    assert root is not None and root.status == TaskStatus.BLOCKED.value
    assert "执行环境" in meta["planner_blocked_recovery_suppressed"]
    assert meta["block_report"]["category"] == "environment"
    assert not any(event.type == "planner.blocked_converted" for event in events)


@pytest.mark.asyncio
async def test_retryable_planner_blocked_waits_instead_of_splitting():
    """可等待重试的阻塞（额度/限流）：**该等就等**，拆子任务只会白烧预算。

    这条与验收路径原有口径一致（`acceptance.blocked` 早就用 `retryable` 判过），
    planner 路径之前漏了 → 同一个 blocked 在两个入口下有两种处置，本轮补齐。
    """
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="rate-limit-planner-root",
                title="Root",
                description="目标明确，但当前被限流。",
                depth=0,
                status=TaskStatus.PLANNED.value,
            )
        )
        db.commit()

    await orchestrator._process_node("rate-limit-planner-root", RateLimitPlannerBlockedRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, "rate-limit-planner-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "rate-limit-planner-root").all()
        meta = loads(root.node_meta_json, {}) if root else {}

    assert children == []
    assert root is not None and root.status == TaskStatus.BLOCKED.value
    assert "可等待重试" in meta["planner_blocked_recovery_suppressed"]
    assert meta["block_report"]["category"] == "rate_limit"


@pytest.mark.asyncio
async def test_acceptance_blocked_on_recovery_node_does_not_recurse():
    """整合验收的递归恢复同样**只允许一层**：已经在恢复链上的节点再 blocked 就停在上报。

    与 planner 侧同一条策略（`blockers.blocked_recovery_suppression`），避免"只修一条路、
    另一条路还能螺旋"。

    结构照生产来：恢复链标记在**恢复子节点**的 scope 里（根节点的 scope[0] 是工程目录，
    不能被标记污染——`_root_target_path` 会拿它当集成目标）。
    """
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    root = TaskNode(
        id="acceptance-chain-root",
        title="Root",
        description="整合已完成子节点的产物。",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
    )
    recovery_parent = TaskNode(
        id="acceptance-chain-recovery",
        parent_id=root.id,
        title="Root：规划阻塞恢复",
        description="把阻塞当成可执行的恢复任务。",
        depth=1,
        status=TaskStatus.WAITING_CHILDREN.value,
        scope_json=dumps(["planner-blocked:deadbeefdeadbeef"]),
    )
    child = TaskNode(
        id="acceptance-chain-child",
        parent_id=recovery_parent.id,
        title="Existing child",
        description="Already done child output.",
        depth=2,
        status=TaskStatus.DONE.value,
        result_json=dumps(
            {
                "worker": WorkerResult(
                    status="succeeded",
                    summary="Done.",
                    changed_files=["existing.txt"],
                    evidence=["existing evidence"],
                ).model_dump(),
                "verifier": VerifierResult(verdict="pass", reason="Verified.").model_dump(),
            }
        ),
    )
    with SessionLocal() as db:
        db.add_all([root, recovery_parent, child])
        db.commit()

    await orchestrator._accept_parent("acceptance-chain-recovery", AcceptanceAlwaysBlockedRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        recovery_parent = db.get(TaskNode, "acceptance-chain-recovery")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "acceptance-chain-recovery").all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "acceptance-chain-recovery").all()
        meta = loads(recovery_parent.node_meta_json, {}) if recovery_parent else {}

    assert len(children) == 1, "只应保留原先那个子节点：不再拆出'整合阻塞恢复'"
    assert recovery_parent is not None and recovery_parent.status == TaskStatus.BLOCKED.value
    assert "阻塞恢复链" in meta["acceptance_blocked_recovery_suppressed"]
    assert not any(event.type == "acceptance.blocked_converted" for event in events)


class SequentialLeafRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.events: list[str] = []

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        await asyncio.sleep(0)
        return envelope("planner", PlannerDecision(decision="execute", reason="Leaf is ready.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        self.events.append(f"work:{task.id}")
        await asyncio.sleep(0)
        return envelope(
            "worker",
            WorkerResult(status="succeeded", summary="Done.", changed_files=[f"{task.id}.txt"], evidence=["evidence"]).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        self.events.append(f"verify:{task.id}")
        await asyncio.sleep(0)
        return envelope("verifier", VerifierResult(verdict="pass", reason="Verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


class PendingVerifierRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.verify_calls = 0

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        raise AssertionError("planner should not run for a pending verifier")

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        raise AssertionError("worker should not rerun when worker result already exists")

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        self.verify_calls += 1
        return envelope("verifier", VerifierResult(verdict="pass", reason="Recovered verifier passed.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


class ExistingChildResumeRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.planned_task_ids: list[str] = []

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        self.planned_task_ids.append(task.id)
        return envelope("planner", PlannerDecision(decision="execute", reason="Existing child is executable.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "worker",
            WorkerResult(status="succeeded", summary="Child done.", changed_files=[f"{task.id}.txt"], evidence=["child evidence"]).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Child verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Parent accepted.").model_dump())


class ResourceBlockedChildRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            payload = PlannerDecision(
                decision="decompose",
                reason="Create one child that will hit capacity.",
                children=[ChildTaskSpec(title="Capacity child", description="Hit rate limit.", acceptance=["Eventually retries."])],
            ).model_dump()
        else:
            payload = PlannerDecision(decision="execute", reason="Leaf is small enough.", leaf_certainty="300%").model_dump()
        return envelope("planner", payload)

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        payload = WorkerResult(
            status="blocked",
            summary="429 rate limit: model capacity exhausted, try again later.",
            risks=["temporary capacity"],
        ).model_dump()
        payload["block_report"] = {
            "category": "rate_limit",
            "retryable": True,
            "parent_action": "wait_and_retry",
            "source": "worker",
        }
        return envelope("worker", payload)

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="blocked", reason="not reached").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="not reached").model_dump())


class DependencyOrderRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.work_order: list[str] = []

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            payload = PlannerDecision(
                decision="decompose",
                reason="Feature depends on Base even though it is listed first.",
                children=[
                    ChildTaskSpec(title="Feature", description="Needs base.", acceptance=["Feature done."], depends_on=["Base"]),
                    ChildTaskSpec(title="Base", description="Create base.", acceptance=["Base done."]),
                ],
            ).model_dump()
        else:
            payload = PlannerDecision(decision="execute", reason="Leaf is small enough.", leaf_certainty="300%").model_dump()
        return envelope("planner", payload)

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        self.work_order.append(task.title)
        return envelope(
            "worker",
            WorkerResult(status="succeeded", summary=f"{task.title} done.", changed_files=[f"{task.id}.txt"], evidence=["evidence"]).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


class PlannerDependencyContextRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.feature_planner_saw_dependency = False

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            payload = PlannerDecision(
                decision="decompose",
                reason="Feature planner must see Base output before deciding.",
                children=[
                    ChildTaskSpec(title="Feature", description="Needs base artifact.", acceptance=["Feature done."], depends_on=["Base"]),
                    ChildTaskSpec(title="Base", description="Create source inventory.", acceptance=["Base artifact exists."]),
                ],
            ).model_dump()
            return envelope("planner", payload)
        if task.title == "Feature":
            dependency_path = Path(task.worktree_path or "") / "docs" / "source_inventory.json"
            self.feature_planner_saw_dependency = dependency_path.is_file()
            if not self.feature_planner_saw_dependency:
                return envelope(
                    "planner",
                    PlannerDecision(decision="blocked", reason="Dependency source_inventory.json is missing.").model_dump(),
                )
        return envelope("planner", PlannerDecision(decision="execute", reason="Leaf is small enough.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        worktree = Path(task.worktree_path or ".")
        if task.title == "Base":
            (worktree / "docs").mkdir(parents=True, exist_ok=True)
            (worktree / "docs" / "source_inventory.json").write_text('{"ok":true}\n', encoding="utf-8")
            changed_files = ["docs/source_inventory.json"]
        else:
            (worktree / "docs").mkdir(parents=True, exist_ok=True)
            (worktree / "docs" / "feature.json").write_text('{"used_dependency":true}\n', encoding="utf-8")
            changed_files = ["docs/feature.json"]
        return envelope(
            "worker",
            WorkerResult(status="succeeded", summary=f"{task.title} done.", changed_files=changed_files, evidence=["evidence"]).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


@pytest.mark.asyncio
async def test_mock_runner_expands_two_layers_and_accepts_parent_nodes():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Build an action game prototype.")
    root_id = await orchestrator.start_project("Build an action game prototype.", constitution_id=constitution_id, auto_run=False)
    await orchestrator.run_tree(root_id, runner_mode="mock")

    with SessionLocal() as db:
        tasks = db.query(TaskNode).all()
        max_depth = max(task.depth for task in tasks)
        root = db.get(TaskNode, root_id)

    assert len(tasks) >= 6
    assert max_depth >= 2
    assert root is not None
    assert root.status == TaskStatus.DONE.value
    assert root.constitution_id == constitution_id


def test_active_frontier_excludes_completed_parent_nodes():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="root-test", title="Root", description="", depth=0, status=TaskStatus.RUNNING.value)
        leaf = TaskNode(
            id="leaf-test",
            parent_id="root-test",
            title="Leaf",
            description="",
            depth=1,
            status=TaskStatus.RUNNING.value,
        )
        db.add_all([root, leaf])
        db.commit()

        tree = orchestrator.get_tree(db)

    active_ids = {task["id"] for task in tree["tasks"] if task["is_active_frontier"]}
    assert "leaf-test" in active_ids
    assert "root-test" not in active_ids
    assert tree["stats"]["active_frontier"] == 1


def test_recover_stale_agent_statuses_marks_old_running_nodes_as_queued():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        stale = TaskNode(id="stale-running", title="Stale", description="", depth=0, status=TaskStatus.WORKING.value)
        waiting = TaskNode(id="waiting-parent", title="Waiting", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        done = TaskNode(id="done-node", title="Done", description="", depth=0, status=TaskStatus.DONE.value)
        db.add_all([stale, waiting, done])
        db.commit()

    recovered = orchestrator.recover_stale_agent_statuses()

    with SessionLocal() as db:
        stale = db.get(TaskNode, "stale-running")
        waiting = db.get(TaskNode, "waiting-parent")
        done = db.get(TaskNode, "done-node")

    assert recovered == 1
    assert stale is not None
    assert waiting is not None
    assert done is not None
    assert stale.status == TaskStatus.QUEUED.value
    assert waiting.status == TaskStatus.WAITING_CHILDREN.value
    assert done.status == TaskStatus.DONE.value


def test_recover_stale_agent_statuses_preserves_live_external_process_statuses(monkeypatch):
    orchestrator = Orchestrator(get_settings())
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=0: [
            {"pid": 10, "task_id": "live-queued-verifier", "status": TaskStatus.VERIFYING.value},
            {"pid": 11, "task_id": "live-working-worker", "status": TaskStatus.WORKING.value},
        ],
    )

    with SessionLocal() as db:
        queued = TaskNode(id="live-queued-verifier", title="Queued verifier", description="", depth=0, status=TaskStatus.QUEUED.value)
        working = TaskNode(id="live-working-worker", title="Working worker", description="", depth=0, status=TaskStatus.WORKING.value)
        stale = TaskNode(id="stale-no-process", title="Stale", description="", depth=0, status=TaskStatus.ACCEPTING.value)
        db.add_all([queued, working, stale])
        db.commit()

    recovered = orchestrator.recover_stale_agent_statuses()

    with SessionLocal() as db:
        queued = db.get(TaskNode, "live-queued-verifier")
        working = db.get(TaskNode, "live-working-worker")
        stale = db.get(TaskNode, "stale-no-process")

    assert recovered == 2
    assert queued is not None and queued.status == TaskStatus.VERIFYING.value
    assert working is not None and working.status == TaskStatus.WORKING.value
    assert stale is not None and stale.status == TaskStatus.QUEUED.value


def test_agent_status_from_codex_command_line_schema_names():
    orchestrator = Orchestrator(get_settings())

    assert orchestrator._agent_status_from_codex_command_line("--output-schema G:\\schemas\\planner-decision.schema.json") == TaskStatus.PLANNING
    assert orchestrator._agent_status_from_codex_command_line("--output-schema G:\\schemas\\worker-result.schema.json") == TaskStatus.WORKING
    assert orchestrator._agent_status_from_codex_command_line("--output-schema G:\\schemas\\verifier-result.schema.json") == TaskStatus.VERIFYING
    assert orchestrator._agent_status_from_codex_command_line("--output-schema G:\\schemas\\acceptance-result.schema.json") == TaskStatus.ACCEPTING


def test_battlefield_agent_command_filter_ignores_codex_sandbox_probe():
    orchestrator = Orchestrator(get_settings())

    assert orchestrator._is_battlefield_agent_codex_command_line(
        "codex.exe --output-schema G:\\schemas\\verifier-result.schema.json "
        "--output-last-message C:\\project\\workspace\\runs\\node-a\\verifier-last-message.json"
    )
    assert not orchestrator._is_battlefield_agent_codex_command_line(
        "codex.exe sandbox -c windows.sandbox=elevated --permissions-profile :read-only "
        "-C C:\\project\\workspace\\worktrees\\node-a Test-Path -LiteralPath docs\\recovery"
    )
    assert not orchestrator._is_battlefield_agent_codex_command_line(
        "codex.exe exec --cd C:\\project\\workspace\\worktrees\\node-a"
    )


def test_get_tree_syncs_live_external_agent_status(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=0: [
            {"pid": 12, "task_id": "live-accepting-parent", "status": TaskStatus.ACCEPTING.value},
        ],
    )

    with SessionLocal() as db:
        root = TaskNode(id="live-sync-root", title="Root", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        parent = TaskNode(
            id="live-accepting-parent",
            parent_id=root.id,
            title="Parent acceptance",
            description="Acceptance process is still running.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, parent])
        db.commit()

        tree = orchestrator.get_tree(db, root.id)
        state = orchestrator.get_watchdog_state(db, root.id)
        synced = db.get(TaskNode, "live-accepting-parent")

    assert synced is not None and synced.status == TaskStatus.ACCEPTING.value
    assert tree["stats"]["active_agent_statuses"] == 1
    assert state["active_agent_statuses"] == 1


def test_tree_defaults_to_latest_root_subtree():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        old_root = TaskNode(id="old-root", title="Old", description="", depth=0, status=TaskStatus.BLOCKED.value, created_at=base_time)
        new_root = TaskNode(
            id="new-root",
            title="New",
            description="",
            depth=0,
            status=TaskStatus.RUNNING.value,
            created_at=base_time + timedelta(seconds=1),
        )
        new_leaf = TaskNode(id="new-leaf", parent_id="new-root", title="Leaf", description="", depth=1, status=TaskStatus.RUNNING.value)
        db.add_all([old_root, new_root, new_leaf])
        db.commit()

        tree = orchestrator.get_tree(db)

    ids = {task["id"] for task in tree["tasks"]}
    assert ids == {"new-root", "new-leaf"}


def test_acceptance_prompt_serializes_child_datetime_values():
    settings = get_settings()
    runner = CodexExecRunner(settings)
    task = TaskNode(
        id="root-datetime",
        title="Parent",
        description="Check child payload serialization.",
        depth=0,
        status=TaskStatus.ACCEPTING.value,
        acceptance_json="[]",
        constitution_json="{}",
    )

    prompt = runner._acceptance_prompt(
        task,
        [{"id": "child-datetime", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}],
    )

    assert "child-datetime" in prompt
    assert "2026-01-01" in prompt


@pytest.mark.asyncio
async def test_root_final_gate_recursively_repairs_missing_godot_entry(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "game-target"
    target.mkdir()

    with SessionLocal() as db:
        root = TaskNode(
            id="final-gate-root",
            title="Root",
            description="开发一个 Godot 2D 项目。",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["最终 Godot 项目必须有可运行主场景。"]),
            constitution_json=dumps({"quality_floor": ["Godot 可玩入口必须存在。"]}),
        )
        child = TaskNode(
            id="final-gate-existing-child",
            parent_id=root.id,
            title="Existing child",
            description="Already done but did not create final entry.",
            depth=1,
            status=TaskStatus.DONE.value,
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": []}}),
        )
        db.add_all([root, child])
        db.commit()

    await orchestrator._accept_parent("final-gate-root", FinalGateRepairRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, "final-gate-root")
        assert root is not None
        children = db.query(TaskNode).filter(TaskNode.parent_id == root.id).all()
        result = loads(root.result_json, {})

    assert root.status == TaskStatus.DONE.value
    assert any(child.title == "最终成品门禁修复：补齐 Godot 可运行入口" for child in children)
    assert result["final_gate"]["passed"] is True
    assert (target / "project.godot").is_file()
    assert (target / "scenes" / "main" / "Main.tscn").is_file()
    assert (target / "scripts" / "player.gd").is_file()


@pytest.mark.asyncio
async def test_root_final_gate_repair_creates_missing_target_directory(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "missing-game-target"

    with SessionLocal() as db:
        root = TaskNode(
            id="final-gate-missing-target-root",
            title="Root",
            description="开发一个 Godot 2D 项目。",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["最终 Godot 项目必须有可运行主场景。"]),
            constitution_json=dumps({"quality_floor": ["Godot 可玩入口必须存在。"]}),
        )
        child = TaskNode(
            id="final-gate-missing-target-child",
            parent_id=root.id,
            title="Existing child",
            description="Already done but target directory has not been created.",
            depth=1,
            status=TaskStatus.DONE.value,
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": []}}),
        )
        db.add_all([root, child])
        db.commit()

    assert not target.exists()

    await orchestrator._accept_parent("final-gate-missing-target-root", FinalGateRepairRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, "final-gate-missing-target-root")
        assert root is not None
        result = loads(root.result_json, {})

    assert root.status == TaskStatus.DONE.value
    assert result["final_gate"]["passed"] is True
    assert target.is_dir()
    assert (target / "project.godot").is_file()
    assert (target / "scenes" / "main" / "Main.tscn").is_file()
    assert (target / "scripts" / "player.gd").is_file()


@pytest.mark.asyncio
async def test_root_without_children_creates_final_gate_repair_before_planner(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "restored-target"
    target.mkdir()
    (target / "README.md").write_text("Restored partial Godot target.", encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(
            id="pre-planner-final-gate-root",
            title="Root",
            description="开发一个 Godot 2D 项目。",
            depth=0,
            status=TaskStatus.QUEUED.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["最终 Godot 项目必须有可运行主场景。"]),
            constitution_json=dumps({"quality_floor": ["Godot 可玩入口必须存在。"]}),
        )
        db.add(root)
        db.commit()

    runner = NoRootPlannerFinalGateRepairRunner()
    await orchestrator._process_node("pre-planner-final-gate-root", runner, asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, "pre-planner-final-gate-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "pre-planner-final-gate-root").all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "pre-planner-final-gate-root").all()
        result = loads(root.result_json, {}) if root else {}

    assert root is not None and root.status == TaskStatus.DONE.value
    assert len(children) == 1
    assert children[0].title == "最终成品门禁修复：补齐 Godot 可运行入口"
    assert children[0].status == TaskStatus.DONE.value
    assert runner.planned_task_ids == [children[0].id]
    assert any(event.type == "final_gate.pre_planner_repair_required" for event in events)
    assert result["final_gate"]["passed"] is True
    assert (target / "project.godot").is_file()
    assert (target / "scenes" / "main" / "Main.tscn").is_file()


@pytest.mark.asyncio
async def test_parent_runs_deterministic_final_gate_before_acceptance_agent(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "pre-acceptance-target"
    target.mkdir()
    (target / "README.md").write_text("Partial restored target.", encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(
            id="pre-acceptance-final-gate-root",
            title="Root",
            description="开发一个 Godot 2D 项目。",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["最终 Godot 项目必须有可运行主场景。"]),
            constitution_json=dumps({"quality_floor": ["Godot 可玩入口必须存在。"]}),
        )
        existing_child = TaskNode(
            id="pre-acceptance-existing-child",
            parent_id=root.id,
            title="Existing done child",
            description="Already done child output.",
            depth=1,
            status=TaskStatus.DONE.value,
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": []}}),
        )
        db.add_all([root, existing_child])
        db.commit()

    runner = CountingAcceptanceFinalGateRepairRunner()
    await orchestrator._accept_parent("pre-acceptance-final-gate-root", runner, asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, "pre-acceptance-final-gate-root")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "pre-acceptance-final-gate-root").all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "pre-acceptance-final-gate-root").all()
        result = loads(root.result_json, {}) if root else {}

    assert root is not None and root.status == TaskStatus.DONE.value
    assert len(children) == 2
    assert any(child.title == "最终成品门禁修复：补齐 Godot 可运行入口" for child in children)
    assert any(event.type == "final_gate.pre_acceptance_repair_required" for event in events)
    assert runner.accepted_task_ids == ["pre-acceptance-final-gate-root"]
    assert result["final_gate"]["passed"] is True
    assert (target / "project.godot").is_file()


def test_parent_integration_copies_child_changed_files(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    child_worktree = tmp_path / "child-worktree"
    target.mkdir()
    child_worktree.mkdir()
    (child_worktree / "GAME_VERTICAL_SLICE_PROBE.md").write_text("probe", encoding="utf-8")

    with SessionLocal() as db:
        parent = TaskNode(
            id="root-integrate",
            title="Parent",
            description="Integrate child output.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
        )
        child = TaskNode(
            id="child-integrate",
            parent_id="root-integrate",
            title="Child",
            description="Create probe file.",
            depth=1,
            status=TaskStatus.DONE.value,
            worktree_path=str(child_worktree),
            result_json=dumps(
                {
                    "worker": {
                        "status": "succeeded",
                        "changed_files": ["GAME_VERTICAL_SLICE_PROBE.md"],
                    }
                }
            ),
        )
        db.add_all([parent, child])
        db.commit()

        orchestrator._integrate_child_outputs(db, parent, [child])
        db.commit()

    assert (target / "GAME_VERTICAL_SLICE_PROBE.md").read_text(encoding="utf-8") == "probe"
    with SessionLocal() as db:
        parent = db.get(TaskNode, "root-integrate")
        assert parent is not None
        parent_meta = loads(parent.node_meta_json, {})
        assert parent_meta["parent_integration"]["target"] == str(target)
        assert parent_meta["parent_integration"]["target_kind"] == "root_target"
        assert parent_meta["parent_integration"]["integrated_count"] == 1


def test_parent_integration_recursively_copies_descendant_changed_files(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    leaf_worktree = tmp_path / "leaf-worktree"
    target.mkdir()
    leaf_worktree.mkdir()
    (leaf_worktree / "scenes" / "actors").mkdir(parents=True)
    (leaf_worktree / "scenes" / "actors" / "Player.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(
            id="recursive-root-integrate",
            title="Root",
            description="Integrate nested child output.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
        )
        direct_parent = TaskNode(
            id="recursive-direct-parent",
            parent_id=root.id,
            title="Direct parent",
            description="Owns nested leaves.",
            depth=1,
            status=TaskStatus.DONE.value,
            result_json=dumps({"acceptance": {"verdict": "pass"}}),
        )
        leaf = TaskNode(
            id="recursive-leaf-output",
            parent_id=direct_parent.id,
            title="Nested leaf",
            description="Create player scene.",
            depth=2,
            status=TaskStatus.DONE.value,
            worktree_path=str(leaf_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["scenes/actors/Player.tscn"]}}),
        )
        db.add_all([root, direct_parent, leaf])
        db.commit()

        orchestrator._integrate_child_outputs(db, root, [direct_parent])
        db.commit()

    assert (target / "scenes" / "actors" / "Player.tscn").read_text(encoding="utf-8") == "[gd_scene format=3]\n"
    with SessionLocal() as db:
        root = db.get(TaskNode, "recursive-root-integrate")
        assert root is not None
        meta = loads(root.node_meta_json, {})
        assert meta["parent_integration"]["integrated_count"] == 1
        event = db.query(EventRecord).filter(EventRecord.task_id == root.id, EventRecord.type == "parent.integration").one()
        payload = loads(event.payload_json, {})
        assert payload["integrated"][0]["child_id"] == "recursive-direct-parent"
        assert payload["integrated"][0]["output_node_id"] == "recursive-leaf-output"


def test_parent_integration_ignores_non_done_descendant_changed_files(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    good_worktree = tmp_path / "good-worktree"
    failed_worktree = tmp_path / "failed-worktree"
    target.mkdir()
    good_worktree.mkdir()
    failed_worktree.mkdir()
    (good_worktree / "README.md").write_text("good", encoding="utf-8")
    (failed_worktree / "BROKEN.md").write_text("broken", encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(
            id="status-filter-root",
            title="Root",
            description="Integrate only accepted outputs.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
        )
        direct_parent = TaskNode(
            id="status-filter-parent",
            parent_id=root.id,
            title="Direct parent",
            description="Owns accepted and failed leaves.",
            depth=1,
            status=TaskStatus.DONE.value,
            result_json=dumps({"acceptance": {"verdict": "pass"}}),
        )
        good_leaf = TaskNode(
            id="status-filter-good-leaf",
            parent_id=direct_parent.id,
            title="Good leaf",
            description="Accepted docs.",
            depth=2,
            status=TaskStatus.DONE.value,
            worktree_path=str(good_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["README.md"]}}),
        )
        failed_leaf = TaskNode(
            id="status-filter-failed-leaf",
            parent_id=direct_parent.id,
            title="Failed leaf",
            description="Failed docs.",
            depth=2,
            status=TaskStatus.FAILED.value,
            worktree_path=str(failed_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["BROKEN.md"]}}),
        )
        db.add_all([root, direct_parent, good_leaf, failed_leaf])
        db.commit()

        orchestrator._integrate_child_outputs(db, root, [direct_parent])
        db.commit()

    assert (target / "README.md").read_text(encoding="utf-8") == "good"
    assert not (target / "BROKEN.md").exists()


def test_acceptance_prompt_names_root_target_as_authoritative_integration_path(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    task = TaskNode(
        id="root-target-acceptance",
        title="Parent",
        description="Accept merged Godot target.",
        depth=1,
        status=TaskStatus.ACCEPTING.value,
        acceptance_json=dumps(["Integrated target must be runnable."]),
        node_meta_json=dumps(
            {
                "parent_integration": {
                    "target": str(target),
                    "target_kind": "root_target",
                    "parent_worktree_path": None,
                    "integrated_count": 3,
                    "skipped_count": 0,
                }
            }
        ),
    )
    runner = CodexExecRunner(get_settings())

    prompt = runner._acceptance_prompt(task, [])

    assert "INTEGRATION_TARGET_KIND: root_target" in prompt
    assert f"INTEGRATION_TARGET_PATH:\n{target}" in prompt
    assert "Do not fail merely because a worktrees/<parent-id> folder is absent" in prompt
    assert "the orchestrator copied child changed_files into the root TARGET_PATH before acceptance" in prompt


class AcceptancePassOnlyRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.accepted_children: list[list[str]] = []

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        raise NotImplementedError

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        self.accepted_children.append([str(child["id"]) for child in children])
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted deliverables.").model_dump())


@pytest.mark.asyncio
async def test_parent_acceptance_does_not_wait_for_queued_skill_note_child(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    child_worktree = tmp_path / "child-worktree"
    target.mkdir()
    child_worktree.mkdir()
    (child_worktree / "scenes").mkdir()
    (child_worktree / "scenes" / "feature.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(
            id="nonblocking-note-root",
            title="Root",
            description="Owns target.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
        )
        parent = TaskNode(
            id="nonblocking-note-parent",
            parent_id=root.id,
            title="Parent",
            description="Integrate real child deliverables.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        real_child = TaskNode(
            id="nonblocking-note-real-child",
            parent_id=parent.id,
            title="Feature scene",
            description="Create a real deliverable.",
            depth=2,
            status=TaskStatus.DONE.value,
            worktree_path=str(child_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["scenes/feature.tscn"]}}),
        )
        skill_note_child = TaskNode(
            id="nonblocking-note-skill-child",
            parent_id=parent.id,
            title="汇总本节点技能笔记",
            description="Auxiliary note.",
            depth=2,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, parent, real_child, skill_note_child])
        db.commit()

    runner = AcceptancePassOnlyRunner()
    await orchestrator._accept_parent("nonblocking-note-parent", runner, asyncio.Semaphore(1))

    assert (target / "scenes" / "feature.tscn").read_text(encoding="utf-8") == "[gd_scene format=3]\n"
    assert runner.accepted_children == [["nonblocking-note-real-child"]]
    with SessionLocal() as db:
        parent = db.get(TaskNode, "nonblocking-note-parent")
        skill_note_child = db.get(TaskNode, "nonblocking-note-skill-child")
        event = db.query(EventRecord).filter(
            EventRecord.task_id == "nonblocking-note-parent",
            EventRecord.type == "parent.skill_note_nonblocking",
        ).one()
    assert parent is not None and parent.status == TaskStatus.DONE.value
    assert parent.progress_total == 1
    assert skill_note_child is not None and skill_note_child.status == TaskStatus.CANCELED.value
    assert loads(skill_note_child.result_json, {})["system"]["status"] == "superseded_by_parent_nonblocking_skill_note"
    assert "nonblocking-note-skill-child" in loads(event.payload_json, {})["settled_skill_note_task_ids"]


@pytest.mark.asyncio
async def test_skill_note_parent_still_waits_for_queued_skill_note_child(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    child_worktree = tmp_path / "child-worktree"
    target.mkdir()
    child_worktree.mkdir()
    (child_worktree / "docs").mkdir()
    (child_worktree / "docs" / "summary.json").write_text("{}", encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(
            id="skill-note-parent-root",
            title="Root",
            description="Owns target.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
        )
        parent = TaskNode(
            id="skill-note-parent",
            parent_id=root.id,
            title="归拢递归经验 Skill",
            description="Skill note parent.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        real_child = TaskNode(
            id="skill-note-parent-real-child",
            parent_id=parent.id,
            title="Evidence summary",
            description="Create note evidence.",
            depth=2,
            status=TaskStatus.DONE.value,
            worktree_path=str(child_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["docs/summary.json"]}}),
        )
        queued_note_child = TaskNode(
            id="skill-note-parent-queued-child",
            parent_id=parent.id,
            title="汇总本节点技能笔记",
            description="Nested note.",
            depth=2,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, parent, real_child, queued_note_child])
        db.commit()

    runner = AcceptancePassOnlyRunner()
    await orchestrator._accept_parent("skill-note-parent", runner, asyncio.Semaphore(1))

    assert runner.accepted_children == []
    assert not (target / "docs" / "summary.json").exists()
    with SessionLocal() as db:
        parent = db.get(TaskNode, "skill-note-parent")
        queued_note_child = db.get(TaskNode, "skill-note-parent-queued-child")
        events = db.query(EventRecord).filter(
            EventRecord.task_id == "skill-note-parent",
            EventRecord.type == "parent.skill_note_nonblocking",
        ).all()
    assert parent is not None and parent.status == TaskStatus.WAITING_CHILDREN.value
    assert parent.progress_total == 2
    assert queued_note_child is not None and queued_note_child.status == TaskStatus.QUEUED.value
    assert events == []


@pytest.mark.asyncio
async def test_parent_godot_integration_gate_repairs_missing_resource_refs(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    child_worktree = tmp_path / "child-worktree"
    target.mkdir()
    child_worktree.mkdir()
    (child_worktree / "scenes" / "main").mkdir(parents=True)
    (child_worktree / "scripts" / "main").mkdir(parents=True)
    (child_worktree / "data" / "levels").mkdir(parents=True)
    (child_worktree / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (child_worktree / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene format=3]\n[node name="Main" type="Node2D"]\n',
        encoding="utf-8",
    )
    (child_worktree / "scripts" / "main" / "MainRoot.gd").write_text("extends Node2D\n", encoding="utf-8")
    (child_worktree / "data" / "levels" / "level_catalog.json").write_text(
        dumps({"levels": [{"id": "dock", "scene_path": "res://scenes/levels/MissingDock.tscn"}]}),
        encoding="utf-8",
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="integration-root",
            title="Root",
            description="Godot root.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
        )
        parent = TaskNode(
            id="integration-parent",
            parent_id=root.id,
            title="Parent Godot integration resource path check",
            description="Run parent integration acceptance and resource reference closure.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
            acceptance_json=dumps(["Godot integration must have no res:// broken links."]),
        )
        child = TaskNode(
            id="integration-child",
            parent_id=parent.id,
            title="Initial integrated files",
            description="Create project with a missing resource reference.",
            depth=2,
            status=TaskStatus.DONE.value,
            worktree_path=str(child_worktree),
            result_json=dumps(
                {
                    "worker": {
                        "status": "succeeded",
                        "changed_files": [
                            "project.godot",
                            "scenes/main/Main.tscn",
                            "scripts/main/MainRoot.gd",
                            "data/levels/level_catalog.json",
                        ],
                    }
                }
            ),
        )
        db.add_all([root, parent, child])
        db.commit()

    await orchestrator._accept_parent("integration-parent", IntegrationGateRepairRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        parent = db.get(TaskNode, "integration-parent")
        assert parent is not None
        repair_children = db.query(TaskNode).filter(TaskNode.parent_id == "integration-parent", TaskNode.id != "integration-child").all()
        result = loads(parent.result_json, {})
        events = db.query(EventRecord).filter(EventRecord.task_id == "integration-parent").all()

    assert parent.status == TaskStatus.DONE.value
    assert repair_children
    assert (target / "scenes" / "levels" / "MissingDock.tscn").is_file()
    assert result["integration_gate"]["passed"] is True
    assert any(event.type == "integration_gate.pre_acceptance_repair_required" for event in events)


def test_arcade_brawler_final_gate_rejects_data_only_skeleton(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "brawler-target"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scripts" / "main").mkdir(parents=True)
    (target / "data" / "characters").mkdir(parents=True)
    (target / "data" / "enemies").mkdir(parents=True)
    (target / "data" / "levels").mkdir(parents=True)
    (target / "data" / "spawns").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text("[gd_scene format=3]\n[node name=\"Main\" type=\"Node2D\"]\n", encoding="utf-8")
    (target / "scripts" / "main" / "MainRoot.gd").write_text("extends Node2D\n", encoding="utf-8")
    (target / "README.md").write_text("启动入口：scenes/main/Main.tscn\n", encoding="utf-8")
    (target / "data" / "characters" / "player_hero.json").write_text(
        dumps(
            {
                "resource_paths": {"scene": "res://scenes/actors/player/Player.tscn"},
                "attacks": [{"id": "jab"}, {"id": "heavy"}, {"id": "skill"}],
            }
        ),
        encoding="utf-8",
    )
    (target / "data" / "enemies" / "enemy_catalog.json").write_text(
        dumps(
            {
                "enemies": [
                    {"id": "grunt", "enemy_type": "grunt", "resource_paths": {"scene": "res://scenes/actors/enemies/Grunt.tscn"}},
                    {"id": "runner", "enemy_type": "charger", "resource_paths": {"scene": "res://scenes/actors/enemies/Runner.tscn"}},
                    {"id": "thrower", "enemy_type": "ranged", "resource_paths": {"scene": "res://scenes/actors/enemies/Thrower.tscn"}},
                    {"id": "guard", "enemy_type": "defender", "resource_paths": {"scene": "res://scenes/actors/enemies/Guard.tscn"}},
                    {"id": "boss", "enemy_type": "boss", "resource_paths": {"scene": "res://scenes/actors/enemies/Boss.tscn"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    (target / "data" / "levels" / "level_catalog.json").write_text(
        dumps(
            {
                "levels": [
                    {"id": "one", "scene_path": "res://scenes/levels/One.tscn"},
                    {"id": "two", "scene_path": "res://scenes/levels/Two.tscn"},
                    {"id": "three", "scene_path": "res://scenes/levels/Three.tscn"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (target / "data" / "spawns" / "arcade_wave_plan.json").write_text(
        dumps({"wave_sets": [{"id": "one"}, {"id": "two"}, {"id": "three"}]}),
        encoding="utf-8",
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="brawler-gate-root",
            title="Root",
            description="开发一个2D 街机 格斗 闯关 游戏 godot 品质和市面上街机一个水平",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["必须是完整街机格斗闯关 Godot 成品。"]),
            constitution_json=dumps({"quality_floor": ["4 个普通敌人、1 个 Boss、3 个关卡、可玩战斗循环。"]}),
        )
        db.add(root)
        db.commit()
        gate = orchestrator._final_root_product_gate(db, root)

    assert gate["required"] is True
    assert gate["passed"] is False
    assert any("玩家可实例化场景" in item for item in gate["missing"])
    assert any("缺少敌人/Boss 场景" in item for item in gate["missing"])
    assert any("缺少关卡场景" in item for item in gate["missing"])
    assert any("战斗" in item for item in gate["missing"])


def test_arcade_brawler_final_gate_rejects_placeholder_only_scenes(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "placeholder-brawler"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scenes" / "actors" / "player").mkdir(parents=True)
    (target / "scenes" / "actors" / "enemies").mkdir(parents=True)
    (target / "scenes" / "levels").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    (target / "data" / "characters").mkdir(parents=True)
    (target / "data" / "enemies").mkdir(parents=True)
    (target / "data" / "levels").mkdir(parents=True)
    (target / "data" / "spawns").mkdir(parents=True)

    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    empty_scene = '[gd_scene format=3]\n[node name="Placeholder" type="Node2D"]\n'
    for scene in [
        target / "scenes" / "main" / "Main.tscn",
        target / "scenes" / "actors" / "player" / "Player.tscn",
        target / "scenes" / "actors" / "enemies" / "Grunt.tscn",
        target / "scenes" / "actors" / "enemies" / "Runner.tscn",
        target / "scenes" / "actors" / "enemies" / "Thrower.tscn",
        target / "scenes" / "actors" / "enemies" / "Guard.tscn",
        target / "scenes" / "actors" / "enemies" / "Boss.tscn",
        target / "scenes" / "levels" / "One.tscn",
        target / "scenes" / "levels" / "Two.tscn",
        target / "scenes" / "levels" / "Three.tscn",
    ]:
        scene.write_text(empty_scene, encoding="utf-8")
    (target / "scripts" / "placeholder.gd").write_text(
        "extends Node2D\n"
        "# combat attack hit health damage player hero enemy ai boss hud health_bar score menu restart pause\n",
        encoding="utf-8",
    )
    (target / "README.md").write_text("Run res://scenes/main/Main.tscn. Arcade brawler combat HUD menu.", encoding="utf-8")
    (target / "data" / "characters" / "player_hero.json").write_text(
        dumps(
            {
                "resource_paths": {"scene": "res://scenes/actors/player/Player.tscn"},
                "attacks": [{"id": "jab"}, {"id": "heavy"}, {"id": "skill"}],
            }
        ),
        encoding="utf-8",
    )
    (target / "data" / "enemies" / "enemy_catalog.json").write_text(
        dumps(
            {
                "enemies": [
                    {"id": "grunt", "enemy_type": "grunt", "resource_paths": {"scene": "res://scenes/actors/enemies/Grunt.tscn"}},
                    {"id": "runner", "enemy_type": "charger", "resource_paths": {"scene": "res://scenes/actors/enemies/Runner.tscn"}},
                    {"id": "thrower", "enemy_type": "ranged", "resource_paths": {"scene": "res://scenes/actors/enemies/Thrower.tscn"}},
                    {"id": "guard", "enemy_type": "defender", "resource_paths": {"scene": "res://scenes/actors/enemies/Guard.tscn"}},
                    {"id": "boss", "enemy_type": "boss", "resource_paths": {"scene": "res://scenes/actors/enemies/Boss.tscn"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    (target / "data" / "levels" / "level_catalog.json").write_text(
        dumps(
            {
                "levels": [
                    {"id": "one", "scene_path": "res://scenes/levels/One.tscn"},
                    {"id": "two", "scene_path": "res://scenes/levels/Two.tscn"},
                    {"id": "three", "scene_path": "res://scenes/levels/Three.tscn"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (target / "data" / "spawns" / "arcade_wave_plan.json").write_text(
        dumps({"wave_sets": [{"id": "one"}, {"id": "two"}, {"id": "three"}]}),
        encoding="utf-8",
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="placeholder-brawler-root",
            title="Root",
            description="Build a Godot arcade brawler beat-em-up game.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["Must be a complete playable Godot arcade brawler."]),
            constitution_json=dumps({"quality_floor": ["Playable combat loop, enemies, boss, levels, HUD, menu."]}),
        )
        db.add(root)
        db.commit()
        gate = orchestrator._final_root_product_gate(db, root)

    failed_names = {check["name"] for check in gate["checks"] if not check["passed"]}
    assert gate["required"] is True
    assert gate["passed"] is False
    assert "brawler_player_scene_behavior" in failed_names
    assert "brawler_enemy_scene_behavior" in failed_names
    assert "brawler_level_scene_content" in failed_names
    assert "brawler_main_scene_behavior" in failed_names
    assert "brawler_player_control_code" in failed_names
    assert "brawler_enemy_ai_code" in failed_names
    assert "brawler_progression_loop_code" in failed_names
    assert "brawler_visual_asset_set" in failed_names
    assert "brawler_visual_assets_referenced" in failed_names
    assert "brawler_audio_asset_set" in failed_names
    assert "brawler_audio_assets_referenced" in failed_names


def test_final_gate_res_reference_check_ignores_json_fragments(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "fragment-project"
    (target / "data" / "spawns").mkdir(parents=True)
    (target / "data" / "levels").mkdir(parents=True)
    (target / "docs").mkdir(parents=True)
    (target / "data" / "spawns" / "arcade_wave_plan.json").write_text('{"wave_sets":[]}', encoding="utf-8")
    (target / "data" / "levels" / "level_catalog.json").write_text(
        dumps(
            {
                "levels": [
                    {
                        "id": "dock",
                        "scene_path": "res://scenes/levels/MissingDock.tscn",
                        "spawn_config": "res://data/spawns/arcade_wave_plan.json#wave_sets/dock",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (target / "docs" / "DATA_FORMATS.md").write_text(
        "Use res://data/spawns/arcade_wave_plan.json#wave_sets/dock for wave data. "
        "Example only: res://path/file.json#section/id`.",
        encoding="utf-8",
    )

    missing = orchestrator._missing_godot_res_references(target)

    assert "res://data/spawns/arcade_wave_plan.json#wave_sets/dock" not in missing
    assert not any(item.startswith("res://path/") for item in missing)
    assert "res://scenes/levels/MissingDock.tscn" in missing


def test_final_gate_res_reference_check_ignores_docs_skill_note_prose(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "docs-prose-project"
    (target / "data" / "levels").mkdir(parents=True)
    (target / "docs" / "skill_notes").mkdir(parents=True)
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=1 format=3]\n[node name="Main" type="Node2D"]\n',
        encoding="utf-8",
    )
    (target / "data" / "levels" / "level_catalog.json").write_text(
        dumps({"levels": [{"id": "dock", "scene_path": "res://scenes/levels/MissingDock.tscn"}]}),
        encoding="utf-8",
    )
    (target / "docs" / "skill_notes" / "node.json").write_text(
        dumps(
            {
                "evidence": [
                    "res://scenes/main/Main.tscn，说明工程入口已配置。",
                    "example only: res://docs/not-runtime/MissingExample.tscn",
                ]
            }
        ),
        encoding="utf-8",
    )

    missing = orchestrator._missing_godot_res_references(target)

    assert "res://scenes/levels/MissingDock.tscn" in missing
    assert "res://scenes/main/Main.tscn" not in missing
    assert not any("MissingExample" in item for item in missing)
    assert not any("说明" in item for item in missing)


def test_final_gate_rejects_empty_referenced_asset_files(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "asset-project"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    (target / "assets" / "ui").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=3 format=3]\n'
        '[ext_resource type="Script" path="res://scripts/Main.gd" id="1"]\n'
        '[ext_resource type="Texture2D" path="res://assets/ui/portrait.png" id="2"]\n'
        '[node name="Main" type="Node2D"]\n'
        'script = ExtResource("1")\n',
        encoding="utf-8",
    )
    (target / "scripts" / "Main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (target / "assets" / "ui" / "portrait.png").write_bytes(b"")
    (target / "README.md").write_text("Run res://scenes/main/Main.tscn.", encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(
            id="asset-gate-root",
            title="Root",
            description="Build a Godot project.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["Must be a runnable Godot project."]),
        )
        db.add(root)
        db.commit()
        gate = orchestrator._final_root_product_gate(db, root)

    failed_names = {check["name"] for check in gate["checks"] if not check["passed"]}
    assert gate["required"] is True
    assert gate["passed"] is False
    assert "referenced_asset_files" in failed_names
    assert any("portrait.png" in item for item in gate["missing"])


def test_final_gate_rejects_unreadable_text_files(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "text-integrity-project"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://scripts/Main.gd" id="1"]\n'
        '[node name="Main" type="Node2D"]\n'
        'script = ExtResource("1")\n',
        encoding="utf-8",
    )
    (target / "scripts" / "Main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (target / "README.md").write_text(
        "\u9416\u8dfa\u7730\u947a\u509c\u5063\u6960\u5c83\u7609\u95ab\u6c33\u7e43\u9286\u4e7bource_files",
        encoding="utf-8",
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="text-integrity-root",
            title="Root",
            description="Build a Godot project.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["Must be a runnable Godot project."]),
        )
        db.add(root)
        db.commit()
        gate = orchestrator._final_root_product_gate(db, root)

    failed_names = {check["name"] for check in gate["checks"] if not check["passed"]}
    repair_children = orchestrator._final_gate_repair_children(root, gate)
    assert gate["required"] is True
    assert gate["passed"] is False
    assert "text_integrity" in failed_names
    assert any("README.md" in item and "mojibake text corruption" in item for item in gate["missing"])
    assert repair_children[0].title == "最终成品门禁修复：清理文本乱码"


def test_final_gate_rejects_stale_skeleton_readme(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "stale-readme-project"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://scripts/Main.gd" id="1"]\n'
        '[node name="Main" type="Node2D"]\n'
        'script = ExtResource("1")\n',
        encoding="utf-8",
    )
    (target / "scripts" / "Main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (target / "README.md").write_text(
        "This is a Godot project skeleton. Player, enemies, UI, and levels will be connected by a future subsystem.",
        encoding="utf-8",
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="stale-readme-root",
            title="Root",
            description="Build a Godot project.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["Must be a runnable Godot project."]),
        )
        db.add(root)
        db.commit()
        gate = orchestrator._final_root_product_gate(db, root)

    failed_names = {check["name"] for check in gate["checks"] if not check["passed"]}
    repair_children = orchestrator._final_gate_repair_children(root, gate)
    assert gate["required"] is True
    assert gate["passed"] is False
    assert "readme_current_playable_scope" in failed_names
    assert any("README.md" in item and "skeleton" in item for item in gate["missing"])
    assert any("readme-current-playable-scope" in child.coverage_ids for child in repair_children)


def test_final_gate_repair_splits_arcade_brawler_content_gaps() -> None:
    orchestrator = Orchestrator(get_settings())
    task = TaskNode(id="split-final-gate", title="Root", description="", depth=0)
    final_gate = {
        "target": "C:/projects/demo-target",
        "missing": [
            "缺少敌人/Boss 场景：street_thug, knife_runner, bottle_thrower, shield_guard, metro_boss。",
            "缺少关卡场景：dock_dawn, market_run, rooftop_boss。",
            (
                "存在无法解析的 res:// 资源引用：res://assets/ui/portraits/player_hero.png, "
                "res://scenes/actors/enemies/StreetThug.tscn, res://scenes/levels/DockDawn.tscn, "
                "res://scenes/projectiles/BottleProjectile.tscn, res://scenes/ui/Victory.tscn。"
            ),
        ],
    }

    children = orchestrator._final_gate_repair_children(task, final_gate)

    titles = [child.title for child in children]
    assert titles == [
        "最终成品门禁修复：补齐敌人和 Boss 场景",
        "最终成品门禁修复：补齐三关战斗场景",
        "最终成品门禁修复：补齐断链资源与流程场景",
    ]
    assert all(child.estimated_leaf is False for child in children)
    assert "StreetThug" in children[0].description
    assert "DockDawn" in children[1].description
    assert "BottleProjectile.tscn" in children[2].description
    assert "StreetThug.tscn" not in children[2].description


def test_final_gate_repair_splits_brawler_asset_gap() -> None:
    orchestrator = Orchestrator(get_settings())
    task = TaskNode(id="split-asset-gate", title="Root", description="", depth=0)
    final_gate = {
        "target": "C:/projects/demo-target",
        "missing": [
            "brawler visual asset set is too thin: found 2 valid image files, need at least 8 player/enemy/level/UI images.",
            "brawler visual assets are not wired into Godot resources: found 0 referenced valid image assets, need at least 4.",
            "brawler audio asset set is too thin: found 0 valid audio files, need at least 2 combat/UI/stage sound assets.",
            "brawler audio assets are not wired into Godot resources: found 0 referenced valid audio assets, need at least 1.",
        ],
    }

    children = orchestrator._final_gate_repair_children(task, final_gate)

    assert len(children) == 1
    assert children[0].title == "Final product gate repair: add arcade brawler visual and audio assets"
    assert "brawler-visual-audio-assets" in children[0].coverage_ids
    assert children[0].estimated_leaf is False


def test_root_with_existing_children_proactively_adds_final_gate_repairs(tmp_path: Path) -> None:
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "proactive-brawler"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    (target / "data" / "characters").mkdir(parents=True)
    (target / "data" / "enemies").mkdir(parents=True)
    (target / "data" / "levels").mkdir(parents=True)
    (target / "data" / "spawns").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://scripts/Main.gd" id="1"]\n'
        '[node name="Main" type="Node2D"]\n'
        'script = ExtResource("1")\n',
        encoding="utf-8",
    )
    (target / "scripts" / "Main.gd").write_text(
        "extends Node2D\n# combat attack hit health damage player hero enemy ai boss hud health_bar score menu restart pause\n",
        encoding="utf-8",
    )
    (target / "README.md").write_text("Godot arcade brawler entry is scenes/main/Main.tscn.", encoding="utf-8")
    (target / "data" / "characters" / "player_hero.json").write_text(
        dumps({"resource_paths": {"scene": "res://scenes/actors/player/Player.tscn"}, "attacks": [{"id": "jab"}, {"id": "heavy"}, {"id": "skill"}]}),
        encoding="utf-8",
    )
    (target / "data" / "enemies" / "enemy_catalog.json").write_text(
        dumps(
            {
                "enemies": [
                    {"id": "street_thug", "enemy_type": "grunt", "resource_paths": {"scene": "res://scenes/actors/enemies/StreetThug.tscn"}},
                    {"id": "knife_runner", "enemy_type": "charger", "resource_paths": {"scene": "res://scenes/actors/enemies/KnifeRunner.tscn"}},
                    {"id": "bottle_thrower", "enemy_type": "ranged", "resource_paths": {"scene": "res://scenes/actors/enemies/BottleThrower.tscn"}},
                    {"id": "shield_guard", "enemy_type": "defender", "resource_paths": {"scene": "res://scenes/actors/enemies/ShieldGuard.tscn"}},
                    {"id": "metro_boss", "enemy_type": "boss", "resource_paths": {"scene": "res://scenes/actors/enemies/MetroBoss.tscn"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    (target / "data" / "levels" / "level_catalog.json").write_text(
        dumps(
            {
                "levels": [
                    {"id": "dock_dawn", "scene_path": "res://scenes/levels/DockDawn.tscn"},
                    {"id": "market_run", "scene_path": "res://scenes/levels/MarketRun.tscn"},
                    {"id": "rooftop_boss", "scene_path": "res://scenes/levels/RooftopBoss.tscn"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (target / "data" / "spawns" / "arcade_wave_plan.json").write_text(
        dumps({"wave_sets": [{"id": "dock"}, {"id": "market"}, {"id": "roof"}]}),
        encoding="utf-8",
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="proactive-final-gate-root",
            title="Root",
            description=ARCADE_BRAWLER_GOAL,
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
            acceptance_json=dumps(["必须是完整街机格斗闯关 Godot 成品。"]),
        )
        existing_child = TaskNode(
            id="proactive-existing-child",
            parent_id=root.id,
            title="已有玩家任务",
            description="Existing player subtree.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, existing_child])
        db.commit()

    with SessionLocal() as db:
        root = db.get(TaskNode, "proactive-final-gate-root")
        assert root is not None
        created_ids = orchestrator._proactive_root_final_gate_children(db, root, list(root.children))
        db.commit()

    with SessionLocal() as db:
        children = db.query(TaskNode).filter(TaskNode.parent_id == "proactive-final-gate-root").all()
        root = db.get(TaskNode, "proactive-final-gate-root")
        events = db.query(EventRecord).filter(EventRecord.task_id == "proactive-final-gate-root").all()
        second_created = orchestrator._proactive_root_final_gate_children(db, root, children) if root else []
        db.commit()

    titles = {child.title for child in children}
    assert len(created_ids) >= 3
    assert "最终成品门禁修复：补齐敌人和 Boss 场景" in titles
    assert "最终成品门禁修复：补齐三关战斗场景" in titles
    assert "最终成品门禁修复：补齐断链资源与流程场景" in titles
    assert second_created == []
    assert any(event.type == "final_gate.proactive_repair_required" for event in events)


def test_parent_godot_integration_gate_trigger_is_scoped_to_integration_context() -> None:
    orchestrator = Orchestrator(get_settings())

    assert not orchestrator._requires_parent_godot_integration_gate("资源引用使用 godot 资源路径，无大量断链。")
    assert orchestrator._requires_parent_godot_integration_gate("父级集成验收与断链检查 godot project.godot res://")
    assert orchestrator._requires_parent_godot_integration_gate("最终集成 godot 主场景无脚本加载错误和明显资源断链。")


def test_parent_godot_integration_gate_rejects_empty_referenced_assets(tmp_path: Path) -> None:
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "integration-assets"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    (target / "assets" / "ui").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=3 format=3]\n'
        '[ext_resource type="Script" path="res://scripts/Main.gd" id="1"]\n'
        '[ext_resource type="Texture2D" path="res://assets/ui/portrait.png" id="2"]\n'
        '[node name="Main" type="Node2D"]\n'
        'script = ExtResource("1")\n',
        encoding="utf-8",
    )
    (target / "scripts" / "Main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (target / "assets" / "ui" / "portrait.png").write_bytes(b"")

    with SessionLocal() as db:
        root = TaskNode(
            id="asset-integration-root",
            title="Root",
            description="Godot root.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
        )
        parent = TaskNode(
            id="asset-integration-parent",
            parent_id=root.id,
            title="父级集成验收与断链检查",
            description="检查 Godot project.godot 和 res:// 资源引用。",
            depth=1,
            status=TaskStatus.ACCEPTING.value,
            acceptance_json=dumps(["父级集成验收必须拒绝空壳资源。"]),
        )
        db.add_all([root, parent])
        db.commit()
        gate = orchestrator._parent_godot_integration_gate(db, parent)

    failed_names = {check["name"] for check in gate["checks"] if not check["passed"]}
    assert gate["required"] is True
    assert gate["passed"] is False
    assert "referenced_asset_files" in failed_names
    assert any("portrait.png" in item for item in gate["missing"])


def test_parent_godot_integration_gate_rejects_unreadable_text_files(tmp_path: Path) -> None:
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "integration-text"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scripts").mkdir(parents=True)
    (target / "data").mkdir(parents=True)
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://scripts/Main.gd" id="1"]\n'
        '[node name="Main" type="Node2D"]\n'
        'script = ExtResource("1")\n',
        encoding="utf-8",
    )
    (target / "scripts" / "Main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (target / "data" / "notes.json").write_text(
        '{\n  "title": "????????????",\n  "summary": "????????????????"\n}\n',
        encoding="utf-8",
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="text-integration-root",
            title="Root",
            description="Godot root.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            scope_json=dumps([str(target)]),
        )
        parent = TaskNode(
            id="text-integration-parent",
            parent_id=root.id,
            title="父级集成验收与断链检查",
            description="检查 Godot project.godot 和 res:// 资源引用。",
            depth=1,
            status=TaskStatus.ACCEPTING.value,
            acceptance_json=dumps(["父级集成验收必须拒绝不可读文本。"]),
        )
        db.add_all([root, parent])
        db.commit()
        gate = orchestrator._parent_godot_integration_gate(db, parent)

    failed_names = {check["name"] for check in gate["checks"] if not check["passed"]}
    repair_children = orchestrator._integration_gate_repair_children(parent, gate)
    assert gate["required"] is True
    assert gate["passed"] is False
    assert "text_integrity" in failed_names
    assert any("data/notes.json" in item and "question-mark text corruption" in item for item in gate["missing"])
    assert repair_children[0].title == "父节点 Godot 集成门禁修复：清理文本乱码"
    assert "资源断链" not in repair_children[0].description


def test_parent_integration_merges_shared_godot_project_files(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    skeleton_worktree = tmp_path / "skeleton-worktree"
    events_worktree = tmp_path / "events-worktree"
    target.mkdir()
    skeleton_worktree.mkdir()
    events_worktree.mkdir()
    (skeleton_worktree / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Battlefield"\nrun/main_scene="res://scenes/main.tscn"\n',
        encoding="utf-8",
    )
    (skeleton_worktree / "README.md").write_text("Skeleton docs", encoding="utf-8")
    (events_worktree / "project.godot").write_text(
        'config_version=5\n\n[autoload]\n\nGameEvents="*res://scripts/game_events.gd"\n',
        encoding="utf-8",
    )
    (events_worktree / "README.md").write_text("Event docs", encoding="utf-8")

    with SessionLocal() as db:
        parent = TaskNode(
            id="root-merge",
            title="Parent",
            description="Merge shared outputs.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            scope_json=dumps([str(target)]),
        )
        skeleton = TaskNode(
            id="child-skeleton",
            parent_id=parent.id,
            title="Skeleton",
            description="Create project skeleton.",
            depth=1,
            status=TaskStatus.DONE.value,
            worktree_path=str(skeleton_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["project.godot", "README.md"]}}),
        )
        events = TaskNode(
            id="child-events",
            parent_id=parent.id,
            title="Events",
            description="Create autoload events.",
            depth=1,
            status=TaskStatus.DONE.value,
            worktree_path=str(events_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["project.godot", "README.md"]}}),
        )
        db.add_all([parent, skeleton, events])
        db.commit()

        orchestrator._integrate_child_outputs(db, parent, [skeleton, events])
        db.commit()

    project_text = (target / "project.godot").read_text(encoding="utf-8")
    readme_text = (target / "README.md").read_text(encoding="utf-8")
    assert 'run/main_scene="res://scenes/main.tscn"' in project_text
    assert 'GameEvents="*res://scripts/game_events.gd"' in project_text
    assert "Skeleton docs" in readme_text
    assert "Event docs" in readme_text


def test_parent_integration_replaces_dangling_godot_input_block(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    parent_worktree = tmp_path / "parent-worktree"
    child_worktree = tmp_path / "child-worktree"
    parent_worktree.mkdir()
    child_worktree.mkdir()
    (parent_worktree / "project.godot").write_text(
        "\n".join(
            [
                "config_version=5",
                "",
                "[input]",
                "",
                "jump={",
                '"deadzone": 0.5,',
                '"events": [Object(InputEventKey,"keycode":32)]',
                "}",
                "gameplay_confirm={",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (child_worktree / "project.godot").write_text(
        "\n".join(
            [
                "config_version=5",
                "",
                "[input]",
                "",
                "jump={",
                '"deadzone": 0.5,',
                '"events": [Object(InputEventKey,"keycode":32)]',
                "}",
                "gameplay_confirm={",
                '"deadzone": 0.5,',
                '"events": [Object(InputEventKey,"keycode":4194309)]',
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with SessionLocal() as db:
        parent = TaskNode(
            id="parent-dangling-input",
            title="Parent",
            description="Repair dangling input.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            worktree_path=str(parent_worktree),
        )
        child = TaskNode(
            id="child-dangling-input",
            parent_id=parent.id,
            title="Child",
            description="Complete gameplay confirm.",
            depth=1,
            status=TaskStatus.DONE.value,
            worktree_path=str(child_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["project.godot"]}}),
        )
        db.add_all([parent, child])
        db.commit()

        orchestrator._integrate_child_outputs(db, parent, [child])
        db.commit()

    project_text = (parent_worktree / "project.godot").read_text(encoding="utf-8")
    assert project_text.count("gameplay_confirm={") == 1
    assert '"events": [Object(InputEventKey,"keycode":4194309)]' in project_text
    assert project_text.count("{") == project_text.count("}")
    assert project_text.rstrip().endswith("}")


@pytest.mark.asyncio
async def test_verifier_repair_converts_leaf_to_parent():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Build an action game prototype.")
    root_id = await orchestrator.start_project("Build an action game prototype.", constitution_id=constitution_id, auto_run=False)
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.description += " [force-verifier-fail]"
        db.commit()

    await orchestrator.run_tree(root_id, runner_mode="mock")

    with SessionLocal() as db:
        tasks = db.query(TaskNode).all()
        converted = [task for task in tasks if task.converted_from_leaf]
        root = db.get(TaskNode, root_id)

    assert root is not None
    assert root.status == TaskStatus.DONE.value
    assert converted
    assert all(task.verification_failures >= 1 for task in converted)


def envelope(kind: str, payload: dict) -> RunnerEnvelope:
    return RunnerEnvelope(
        command=f"test {kind}",
        exit_code=0,
        logs_path=f"logs/{kind}.jsonl",
        prompt_path=f"prompts/{kind}.txt",
        last_message_path=f"last/{kind}.json",
        workdir=f"workdirs/{kind}",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_real_runner_verifier_uses_command_capable_sandbox():
    class SpyRunner(CodexExecRunner):
        def __init__(self, settings):
            super().__init__(settings)
            self.calls: list[tuple[str, str, str, str]] = []

        async def _run_codex(self, task: TaskNode, kind: str, prompt: str, schema_name: str, sandbox: str) -> RunnerEnvelope:
            self.calls.append((kind, schema_name, sandbox, prompt))
            return RunnerEnvelope(command="spy", exit_code=0, logs_path=None, payload={"verdict": "pass", "reason": "ok"})

    runner = SpyRunner(get_settings())
    task = TaskNode(id="verifier-sandbox", title="Verifier sandbox", description="Check.", depth=1)

    await runner.verify(task, WorkerResult(status="succeeded", summary="done"))

    assert runner.calls
    kind, schema_name, sandbox, prompt = runner.calls[0]
    assert kind == "verifier"
    assert schema_name == "verifier-result.schema.json"
    assert sandbox == "workspace-write"
    assert "command-capable workspace-write sandbox" in prompt
    assert "Do not create, edit, delete, move, format, or repair files" in prompt


@pytest.mark.asyncio
async def test_real_runner_acceptance_uses_command_capable_sandbox():
    class SpyRunner(CodexExecRunner):
        def __init__(self, settings):
            super().__init__(settings)
            self.calls: list[tuple[str, str, str, str]] = []

        async def _run_codex(self, task: TaskNode, kind: str, prompt: str, schema_name: str, sandbox: str) -> RunnerEnvelope:
            self.calls.append((kind, schema_name, sandbox, prompt))
            return RunnerEnvelope(command="spy", exit_code=0, logs_path=None, payload={"verdict": "pass", "reason": "ok"})

    runner = SpyRunner(get_settings())
    task = TaskNode(id="acceptance-sandbox", title="Acceptance sandbox", description="Check.", depth=1)

    await runner.accept(task, [])

    assert runner.calls
    kind, schema_name, sandbox, prompt = runner.calls[0]
    assert kind == "acceptance"
    assert schema_name == "acceptance-result.schema.json"
    assert sandbox == "workspace-write"
    assert "command-capable workspace-write sandbox" in prompt
    assert "Do not create, edit, delete, move, format, or repair files" in prompt


def test_real_runner_prompts_treat_worktree_as_leaf_write_boundary():
    runner = CodexExecRunner(get_settings())
    task = TaskNode(
        id="prompt-boundary",
        title="Boundary check",
        description="Create GAME_VERTICAL_SLICE_PROBE.md.",
        depth=1,
        status=TaskStatus.PLANNED.value,
        scope_json=dumps(["G:\\root-target"]),
        acceptance_json=dumps(["File exists."]),
        constitution_hash="hash",
        constitution_json=dumps({"leaf_rules": ["write deliverables without escaping the assigned worktree"]}),
        worktree_path="C:\\project\\workspace\\worktrees\\prompt-boundary",
    )

    planner_prompt = runner._planner_prompt(task)
    verifier_prompt = runner._verifier_prompt(
        task,
        WorkerResult(status="succeeded", summary="done", changed_files=["GAME_VERTICAL_SLICE_PROBE.md"]),
    )
    constitution_prompt = runner._constitution_prompt("Build a probe.", "G:\\root-target")

    assert "assigned WORKING_DIRECTORY" in planner_prompt
    assert "WORKING_DIRECTORY is this leaf's authoritative writable repository/worktree" in verifier_prompt
    assert "Do not fail only because a file was created in WORKING_DIRECTORY instead of the original root TARGET_PATH" in verifier_prompt
    assert "TARGET_PATH is the original root repository or workspace context" in constitution_prompt
    assert "Leaf rules must require deliverables relative to the assigned WORKING_DIRECTORY" in constitution_prompt
    assert "encoding='utf-8'" in runner._worker_prompt(task)
    assert "every external command you start" in runner._worker_prompt(task)
    assert "Godot/headless validation hangs" in verifier_prompt
    assert "GODOT_BIN" in runner._worker_prompt(task)
    assert "GODOT_BIN" in verifier_prompt
    assert "GODOT_BIN" in runner._acceptance_prompt(task, [])
    assert "Use Chinese for user-visible reason, checks, evidence" in runner._acceptance_prompt(task, [])
    assert "timeout wrapper" in runner._worker_prompt(task)
    assert "timeout wrapper" in verifier_prompt
    assert "timeout wrapper" in runner._acceptance_prompt(task, [])
    assert "Windows shell rule" in runner._worker_prompt(task)
    assert "Windows shell rule" in verifier_prompt
    assert "Windows shell rule" in runner._acceptance_prompt(task, [])
    assert "Codex CLI already executes each command through the Windows shell" in verifier_prompt
    assert "Do not wrap routine validation or file checks in another `powershell.exe -Command ...`" in runner._worker_prompt(task)
    assert "Only invoke `powershell.exe -NoProfile -File script.ps1`" in verifier_prompt
    assert "Do not redirect to `NUL`" in runner._worker_prompt(task)
    assert "Run direct commands instead" in verifier_prompt
    assert "explicitly use `powershell.exe -NoProfile -Command ...`" not in runner._worker_prompt(task)
    assert "Do not run git status or git diff unless `.git` is present" in runner._worker_prompt(task)
    assert "avoid wrapping complex python -c snippets inside PowerShell" in verifier_prompt
    assert "Do not run inline `python -c` through nested shell quoting" in runner._worker_prompt(task)
    assert "Do not run inline `python -c` through nested shell quoting" in verifier_prompt
    assert "Do not run inline `python -c` through nested shell quoting" in runner._acceptance_prompt(task, [])
    assert "python -m json.tool file.json" in verifier_prompt
    assert "rg` is for ASCII tokens, paths, and line numbers" in verifier_prompt
    assert "not for quoting Chinese prose" in runner._worker_prompt(task)
    assert "not for quoting Chinese prose" in runner._acceptance_prompt(task, [])
    assert "Do not use `$null`, `$LASTEXITCODE`" in runner._worker_prompt(task)
    assert "Do not use `$null`, `$LASTEXITCODE`" in verifier_prompt
    assert "Avoid ad-hoc `python -m py_compile`" in runner._worker_prompt(task)
    assert "locked `__pycache__` files" in verifier_prompt
    assert "locked `__pycache__` files" in runner._acceptance_prompt(task, [])
    assert "Do not pass wildcard path arguments such as `docs\\*.json`" in runner._worker_prompt(task)
    assert "Format-Hex -Path file | Select-Object -First 1" in verifier_prompt
    assert "Format-Hex -Path file | Select-Object -First 1" in runner._acceptance_prompt(task, [])
    assert "Never use Bash heredoc syntax" in verifier_prompt
    assert "rg --files" in verifier_prompt
    assert "Avoid complex rg alternation" in verifier_prompt
    assert "Do not spend tokens deleting generated temp/cache folders" in runner._worker_prompt(task)
    assert "Do not spend tokens deleting generated temp/cache folders" in verifier_prompt
    assert "Do not spend tokens deleting generated temp/cache folders" in runner._acceptance_prompt(task, [])
    assert "do not spend the run probing whether optional tools exist" in verifier_prompt
    assert "Read-only validation rule" in verifier_prompt
    assert "always use `powershell.exe -NoProfile -Command ...`" not in verifier_prompt
    assert "Do not use `python -c`" in verifier_prompt
    assert "If one command is rejected by policy" in verifier_prompt
    assert "Do not return pass solely from WORKER_RESULT_JSON" in verifier_prompt
    assert "Do not run git status or git diff unless `.git` is present" in verifier_prompt
    assert "Read-only validation rule" in runner._acceptance_prompt(task, [])
    assert "Do not use PowerShell Get-Content" in verifier_prompt
    assert "or type for Chinese/user-visible file bodies" in verifier_prompt
    assert "Do not dump Chinese file bodies" in verifier_prompt
    assert "Readable text integrity helper" in runner._worker_prompt(task)
    assert "assert_no_match.py" in verifier_prompt
    assert "--fail-on-mojibake" in verifier_prompt
    assert "mojibake" in runner._acceptance_prompt(task, [])
    assert "it exits 0 when no matches are found" in runner._acceptance_prompt(task, [])
    assert "Compact JSON validation helper" in runner._worker_prompt(task)
    assert "json_check.py" in verifier_prompt
    assert "large evidence files do not flood the run log" in runner._acceptance_prompt(task, [])
    assert "valid non-empty files with the correct file header" in runner._worker_prompt(task)
    assert "referenced image/audio assets" in verifier_prompt
    assert "Placeholder assets are acceptable only when they are valid non-empty files" in runner._acceptance_prompt(task, [])
    assert "Valid placeholder asset helper" in runner._worker_prompt(task)
    assert "Valid placeholder asset helper" in verifier_prompt
    assert "Valid placeholder asset helper" in runner._acceptance_prompt(task, [])
    assert "Skill/KM self-rescue rule" in runner._worker_prompt(task)
    # worker prompt 里嵌入的是**配置的** skill_km_url，不再是硬编码内网地址
    assert "skill-library.example" in runner._worker_prompt(task).lower()
    assert "Skill/KM self-rescue rule" in verifier_prompt
    assert "Visual validation rule" in runner._worker_prompt(task)
    assert "verdict=visual_uncertain" in verifier_prompt
    assert "subjective quality" in runner._acceptance_prompt(task, [])
    assert "PROJECT_HOT_LOADED_SKILL_CONTENT" in runner._worker_prompt(task)
    assert "Codex Battlefield Recursive" in runner._worker_prompt(task)
    assert "Parent Skill-Note Task" in runner._acceptance_prompt(task, [])
    assert "Text integrity rule" in runner._worker_prompt(task)
    assert "repeated `????` corruption" in verifier_prompt
    assert "\\uXXXX" in runner._worker_prompt(task)


def test_real_runner_adds_parent_skill_note_child_for_merge():
    settings = get_settings()
    settings = settings.__class__(
        runner_mode="real",
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
    )
    orchestrator = Orchestrator(settings)
    specs = [
        ChildTaskSpec(title="实现玩家移动", description="Move work."),
        ChildTaskSpec(title="实现攻击判定", description="Attack work."),
    ]

    with SessionLocal() as db:
        parent = TaskNode(id="skill-note-parent", title="父任务", description="Parent", depth=0, status=TaskStatus.PLANNED.value)
        db.add(parent)
        db.commit()

        child_ids = orchestrator._create_children(db, parent, specs)
        db.commit()
        children = db.query(TaskNode).filter(TaskNode.parent_id == parent.id).order_by(TaskNode.created_at).all()

    assert len(child_ids) == 3
    skill_child = children[-1]
    meta = loads(skill_child.node_meta_json, {})
    assert meta["coverage_ids"] == ["codex-battlefield-skill-note"]
    assert meta["verification_kind"] == "skill-note consolidation"
    assert len(meta["depends_on_ids"]) == 2
    assert "docs/skill_notes" in skill_child.description


def test_readonly_parent_does_not_get_skill_note_child_and_records_why():
    """只读约束下**不建** skill note 子任务，并且必须落一条事件写明命中了哪句约束。

    这是"少建一个子任务"——不写证据就等于静默放宽（AGENTS 硬约定 4）。判据的来历见
    `docs/spec-capability-benchmark.md` §4.2：unsolvable 任务的"不许新增任何文件"与 skill-note
    验收"必须生成 docs/skill_notes/<TASK_ID>.json"互相打架，真实档里 planner 只能报"约束内部矛盾"。
    """
    settings = get_settings()
    settings = settings.__class__(runner_mode="real", mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    specs = [ChildTaskSpec(title="实现切片", description="Do work.")]

    with SessionLocal() as db:
        parent = TaskNode(
            id="readonly-skill-note-parent",
            title="只读父任务",
            description="Parent",
            depth=0,
            status=TaskStatus.PLANNED.value,
            node_meta_json=dumps({"inherited_constraints": ["不许修改、新增、删除任何文件（测试文件也不例外）"]}),
        )
        db.add(parent)
        db.commit()
        child_ids = orchestrator._create_children(db, parent, specs)
        db.commit()
        children = db.query(TaskNode).filter(TaskNode.parent_id == parent.id).all()
        events = (
            db.query(EventRecord)
            .filter(EventRecord.task_id == parent.id, EventRecord.type == "parent.skill_note_readonly_exempted")
            .all()
        )

    assert len(child_ids) == 1, "只读约束下不该再追加 skill note 子任务"
    assert all("归拢递归经验 Skill" not in child.title for child in children)
    assert len(events) == 1
    assert "任何文件" in json.loads(events[0].payload_json)["matched_constraint"]


def test_parent_skill_notes_are_consolidated_and_similar_entries_merge(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(
        runner_mode="real",
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
    )
    orchestrator = Orchestrator(settings)
    worktree = tmp_path / "worktree"
    notes_dir = worktree / "docs" / "skill_notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "child_a.json").write_text(
        dumps(
            {
                "rules": [
                    {
                        "category": "windows_command",
                        "problem": "嵌套 PowerShell 中使用 python -c 会因为引号丢失失败",
                        "solution": "改用 python -m json.tool 或真实脚本文件",
                        "evidence": ["node-a"],
                        "promote_to_project_skill": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (notes_dir / "child_b.json").write_text(
        dumps(
            {
                "pitfalls": [
                    {
                        "category": "windows_command",
                        "problem": "PowerShell 嵌套 python -c 字典和路径时经常被外层引号破坏",
                        "solution": "使用 python -m json.tool 或落地脚本，不在嵌套命令里写复杂 Python",
                        "evidence": ["node-b", "stale powershell.exe -NoProfile -Command guidance"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with SessionLocal() as db:
        parent = TaskNode(
            id="skill-note-merge-parent",
            title="父任务",
            description="Parent",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            worktree_path=str(worktree),
        )
        db.add(parent)
        db.commit()

        digest = orchestrator._consolidate_parent_skill_notes(db, parent)
        db.commit()

    output = notes_dir / "skill-note-merge-parent_merged.json"
    merged = loads(output.read_text(encoding="utf-8"), {})
    assert digest is not None
    assert output.is_file()
    assert merged["source_entry_count"] == 2
    assert merged["merged_rule_count"] == 1
    assert merged["duplicates_or_similar_merged"] == 1
    assert merged["merged_rules"][0]["source_count"] == 2
    assert merged["merged_rules"][0]["category"] == "windows_command_validation"
    assert "do not wrap routine checks" in merged["merged_rules"][0]["solution"]
    assert "powershell.exe -NoProfile -Command" not in merged["merged_rules"][0]["solution"]
    assert any("Normalized stale" in item for item in merged["merged_rules"][0]["evidence"])
    assert merged["promote_to_project_skill"]


def test_existing_waiting_parent_backfills_skill_note_child_once():
    settings = get_settings()
    settings = settings.__class__(
        runner_mode="real",
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(id="skill-note-backfill-parent", title="父任务", description="Parent", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        child_a = TaskNode(id="skill-note-backfill-a", parent_id=parent.id, title="子任务 A", description="A", depth=1, status=TaskStatus.DONE.value)
        child_b = TaskNode(id="skill-note-backfill-b", parent_id=parent.id, title="子任务 B", description="B", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([parent, child_a, child_b])
        db.commit()

    first = orchestrator.backfill_skill_note_children("skill-note-backfill-parent")
    second = orchestrator.backfill_skill_note_children("skill-note-backfill-parent")

    with SessionLocal() as db:
        children = db.query(TaskNode).filter(TaskNode.parent_id == "skill-note-backfill-parent").order_by(TaskNode.created_at).all()
        skill_children = [child for child in children if orchestrator._is_skill_note_task(child)]
        events = db.query(EventRecord).filter(EventRecord.task_id == "skill-note-backfill-parent", EventRecord.type == "parent.skill_note_backfilled").all()

    assert first["count"] == 1
    assert second["count"] == 0
    assert len(skill_children) == 1
    meta = loads(skill_children[0].node_meta_json, {})
    assert sorted(meta["depends_on_ids"]) == ["skill-note-backfill-a", "skill-note-backfill-b"]
    assert events


@pytest.mark.asyncio
async def test_skill_note_dependency_blocked_sibling_still_runs_for_report():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(id="skill-note-blocked-parent", title="父任务", description="Parent", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="skill-note-blocked-child",
            parent_id=parent.id,
            title="阻塞兄弟",
            description="Blocked.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message="需要人工登录。",
        )
        skill = TaskNode(
            id="skill-note-report-child",
            parent_id=parent.id,
            title="父任务：归拢递归经验 Skill",
            description="Summarize blocked sibling.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps(
                {
                    "coverage_ids": ["codex-battlefield-skill-note"],
                    "verification_kind": "skill-note consolidation",
                    "depends_on_ids": [blocked.id],
                }
            ),
        )
        db.add_all([parent, blocked, skill])
        db.commit()

    await orchestrator._process_children_with_dependencies(parent.id, [skill.id], SlowPlanningRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        skill = db.get(TaskNode, "skill-note-report-child")

    assert skill is not None
    assert skill.status == TaskStatus.DONE.value


def test_placeholder_asset_tool_generates_valid_png_and_wav(tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    runner = CodexExecRunner(settings)
    tool = runner._ensure_placeholder_asset_tool()
    png = tmp_path / "portrait.png"
    wav = tmp_path / "hit.wav"

    png_result = subprocess.run([sys.executable, str(tool), "--output", str(png)], capture_output=True, text=True, timeout=15)
    wav_result = subprocess.run([sys.executable, str(tool), "--output", str(wav)], capture_output=True, text=True, timeout=15)
    env = runner._codex_env()

    assert png_result.returncode == 0, png_result.stderr
    assert wav_result.returncode == 0, wav_result.stderr
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    wav_head = wav.read_bytes()[:12]
    assert wav_head[:4] == b"RIFF"
    assert wav_head[8:12] == b"WAVE"
    assert env["CODEX_BATTLEFIELD_ASSET_TOOL"] == str(tool)


def test_real_runner_sets_utf8_subprocess_environment():
    runner = CodexExecRunner(get_settings())
    env = runner._codex_env()

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["CODEX_BATTLEFIELD_TEXT_ENCODING"] == "utf-8"
    assert Path(env["CODEX_BATTLEFIELD_ASSERT_NO_MATCH_TOOL"]).is_file()
    assert Path(env["CODEX_BATTLEFIELD_JSON_CHECK_TOOL"]).is_file()


def test_real_runner_sets_dedicated_codex_home_for_child_cli(tmp_path: Path):
    codex_home = tmp_path / "battlefield-codex-home"
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace", codex_home=str(codex_home))
    runner = CodexExecRunner(settings)
    env = runner._codex_env()

    assert env["CODEX_HOME"] == str(codex_home)


def test_assert_no_match_helper_returns_success_for_absence(tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    runner = CodexExecRunner(settings)
    tool = runner._ensure_assert_no_match_tool()
    clean = tmp_path / "clean.md"
    corrupt = tmp_path / "corrupt.md"
    clean.write_text("readable utf-8 text\n", encoding="utf-8")
    corrupt.write_text("broken ??? text\n", encoding="utf-8")

    clean_result = subprocess.run(
        [sys.executable, str(tool), str(clean), "--pattern", "???", "--include", "*.md"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    corrupt_result = subprocess.run(
        [sys.executable, str(tool), str(tmp_path), "--pattern", "???", "--include", "*.md"],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert clean_result.returncode == 0, clean_result.stderr
    assert '"ok": true' in clean_result.stdout
    assert corrupt_result.returncode == 1
    assert '"ok": false' in corrupt_result.stdout
    assert "corrupt.md" in corrupt_result.stdout


def test_assert_no_match_helper_fails_on_mojibake_when_enabled(tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    runner = CodexExecRunner(settings)
    tool = runner._ensure_assert_no_match_tool()
    clean = tmp_path / "clean.md"
    corrupt = tmp_path / "corrupt.md"
    clean.write_text("父层节点验收通过，中文可读。\n", encoding="utf-8")
    corrupt.write_text("鐖跺眰鑺傜偣楠岃瘉閫氳繃銆乻ource_files\n", encoding="utf-8")

    clean_result = subprocess.run(
        [sys.executable, str(tool), str(clean), "--fail-on-mojibake", "--include", "*.md"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    corrupt_result = subprocess.run(
        [sys.executable, str(tool), str(tmp_path), "--fail-on-mojibake", "--include", "*.md"],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert clean_result.returncode == 0, clean_result.stderr
    assert '"ok": true' in clean_result.stdout
    assert corrupt_result.returncode == 1
    assert '"mojibake_matches"' in corrupt_result.stdout
    assert "corrupt.md" in corrupt_result.stdout


def test_json_check_helper_validates_without_dumping_body(tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    runner = CodexExecRunner(settings)
    tool = runner._ensure_json_check_tool()
    valid = tmp_path / "valid.json"
    invalid = tmp_path / "invalid.json"
    valid.write_text('{"alpha": 1, "beta": {"nested": true}}', encoding="utf-8")
    invalid.write_text('{"alpha": ', encoding="utf-8")

    valid_result = subprocess.run(
        [sys.executable, str(tool), str(valid)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    invalid_result = subprocess.run(
        [sys.executable, str(tool), str(invalid)],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert valid_result.returncode == 0, valid_result.stderr
    assert '"ok": true' in valid_result.stdout
    assert '"keys": ["alpha", "beta"]' in valid_result.stdout
    assert "nested" not in valid_result.stdout
    assert invalid_result.returncode == 1
    assert "json_decode_error" in invalid_result.stdout


def test_real_runner_injects_configured_godot_binary(tmp_path: Path):
    godot_bin = tmp_path / "Godot_v4.6.3-stable_win64_console.exe"
    godot_bin.write_text("fake", encoding="utf-8")
    settings = get_settings().__class__(godot_bin=str(godot_bin), workspace_dir=get_settings().workspace_dir)
    runner = CodexExecRunner(settings)

    env = runner._codex_env()

    assert env["CODEX_BATTLEFIELD_REAL_GODOT_BIN"] == str(godot_bin)
    assert env["CODEX_BATTLEFIELD_GODOT_TIMEOUT_SECONDS"] == str(settings.godot_timeout_seconds)
    assert env["CODEX_BATTLEFIELD_GODOT_STATIC_FALLBACK"] == "1"
    assert env["CODEX_BATTLEFIELD_GODOT_WRAPPER_SCRIPT"] == str(Path(env["GODOT_BIN"]).with_name("godot_with_timeout.py"))
    assert env["GODOT_BIN"] != str(godot_bin)
    assert env["GODOT4_BIN"] == env["GODOT_BIN"]
    assert Path(env["GODOT_BIN"]).exists()
    assert Path(env["GODOT_BIN"]).name.startswith("godot-with-timeout")
    assert str(tmp_path) in env["PATH"].split(os.pathsep)
    wrapper_script = Path(env["GODOT_BIN"]).with_name("godot_with_timeout.py").read_text(encoding="utf-8")
    assert "taskkill" in wrapper_script
    assert "Stop-Process" in wrapper_script
    assert "timeout = min(timeout, 45)" in wrapper_script
    assert "GODOT_STATIC_CHECK" in wrapper_script
    assert "--user-data-dir" in wrapper_script


def test_real_runner_prompts_literal_godot_wrapper_command(tmp_path: Path):
    godot_bin = tmp_path / "Godot_v4.6.3-stable_win64_console.exe"
    godot_bin.write_text("fake", encoding="utf-8")
    workspace = tmp_path / "battlefield-workspace"
    settings = get_settings().__class__(godot_bin=str(godot_bin), workspace_dir=workspace)
    runner = CodexExecRunner(settings)
    task = TaskNode(
        id="godot-guidance",
        title="Godot guidance",
        description="Validate a Godot project.",
        depth=1,
        status=TaskStatus.PLANNED.value,
        scope_json=dumps([]),
        acceptance_json=dumps(["Godot static check passes."]),
        constitution_hash="hash",
        constitution_json=dumps({}),
        worktree_path=str(tmp_path / "worktree"),
    )
    expected_command = f'python "{workspace / "tools" / "godot_with_timeout.py"}" --headless --path . --quit'

    worker_prompt = runner._worker_prompt(task)
    verifier_prompt = runner._verifier_prompt(task, WorkerResult(status="succeeded", summary="done"))
    acceptance_prompt = runner._acceptance_prompt(task, [])

    assert expected_command in worker_prompt
    assert expected_command in verifier_prompt
    assert expected_command in acceptance_prompt
    assert "Do not wrap it in PowerShell conditionals" in verifier_prompt
    assert "do not run `if ($env:GODOT_BIN) ...`" in verifier_prompt
    assert "do not invoke `& $env:GODOT_BIN`" in verifier_prompt
    assert "Do not set CODEX_BATTLEFIELD_GODOT_STATIC_FALLBACK=0" in verifier_prompt


def test_real_runner_prompts_include_existing_project_context_summary(tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    runner = CodexExecRunner(settings)
    target = tmp_path / "existing-godot-target"
    (target / "scenes" / "main").mkdir(parents=True)
    (target / "scripts").mkdir()
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (target / "scenes" / "main" / "Main.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    (target / "README.md").write_text("现有骨架：主场景和流程入口已经存在。", encoding="utf-8")

    planner_task = TaskNode(
        id="context-planner",
        title="Context planner",
        description="Continue a restored Godot project.",
        depth=0,
        status=TaskStatus.QUEUED.value,
        scope_json=dumps([str(target)]),
        acceptance_json=dumps(["Continue from existing target files."]),
        constitution_json=dumps({"target_path": str(target)}),
    )

    planner_prompt = runner._planner_prompt(planner_task)

    assert "EXISTING_PROJECT_CONTEXT" in planner_prompt
    assert f"CONSTITUTION_TARGET_PATH: {target.resolve()}" in planner_prompt
    assert "- project.godot" in planner_prompt
    assert "- scenes/main/Main.tscn" in planner_prompt
    assert 'run/main_scene="res://scenes/main/Main.tscn"' in planner_prompt
    assert "现有骨架" in planner_prompt

    worktree = tmp_path / "child-worktree"
    worktree.mkdir()
    (worktree / "child_note.txt").write_text("child context", encoding="utf-8")
    worker_task = TaskNode(
        id="context-worker",
        title="Context worker",
        description="Work inside seeded child worktree.",
        depth=1,
        status=TaskStatus.QUEUED.value,
        scope_json=dumps([str(target)]),
        acceptance_json=dumps(["Use existing worktree files."]),
        constitution_json=dumps({"target_path": str(target)}),
        worktree_path=str(worktree),
    )

    worker_prompt = runner._worker_prompt(worker_task)

    assert f"WORKING_DIRECTORY: {worktree.resolve()}" in worker_prompt
    assert "- child_note.txt" in worker_prompt
    assert f"CONSTITUTION_TARGET_PATH: {target.resolve()}" in worker_prompt


def test_real_runner_resolves_relative_scope_paths_against_worktree(tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    runner = CodexExecRunner(settings)
    worktree = tmp_path / "child-worktree"
    scoped_file = worktree / "scenes" / "actors" / "player" / "Player.tscn"
    scoped_file.parent.mkdir(parents=True)
    scoped_file.write_text("[gd_scene format=3]\n[node name=\"Player\" type=\"Node2D\"]\n", encoding="utf-8")
    task = TaskNode(
        id="relative-scope-worker",
        title="Relative scope worker",
        description="Read a scoped scene file.",
        depth=4,
        status=TaskStatus.QUEUED.value,
        scope_json=dumps(["scenes/actors/player/Player.tscn", "parent skill-note merge"]),
        acceptance_json=dumps(["Use the scoped Player scene."]),
        worktree_path=str(worktree),
    )

    worker_prompt = runner._worker_prompt(task)

    assert f"TASK_SCOPE_PATH: {scoped_file.resolve()}" in worker_prompt
    assert "STATUS: file" in worker_prompt
    assert "FILE_BYTES:" in worker_prompt
    assert "TASK_SCOPE_PATH: parent skill-note merge" not in worker_prompt
    assert "codex-battlefield\\api\\scenes\\actors\\player\\Player.tscn" not in worker_prompt


def test_planner_resolves_relative_scope_paths_against_constitution_target_without_worktree(tmp_path: Path):
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    runner = CodexExecRunner(settings)
    target = tmp_path / "root-target"
    scoped_file = target / "scenes" / "actors" / "player" / "Player.tscn"
    scoped_file.parent.mkdir(parents=True)
    scoped_file.write_text("[gd_scene format=3]\n[node name=\"Player\" type=\"CharacterBody2D\"]\n", encoding="utf-8")
    (target / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    task = TaskNode(
        id="relative-scope-planner",
        title="Relative scope planner",
        description="Regenerate reports from scenes/actors/player/Player.tscn.",
        depth=6,
        status=TaskStatus.QUEUED.value,
        scope_json=dumps([
            "scenes/actors/player/Player.tscn",
            "docs/player_tscn_structure_parse.json",
            "docs/player_tscn_resource_node_parse.json",
            "docs/player_connection_audit.json",
        ]),
        acceptance_json=dumps(["Use the scoped Player scene from the root target."]),
        constitution_json=dumps({"target_path": str(target)}),
    )

    planner_prompt = runner._planner_prompt(task)

    assert f"CONSTITUTION_TARGET_PATH: {target.resolve()}" in planner_prompt
    assert f"TASK_SCOPE_PATH: {scoped_file.resolve()}" in planner_prompt
    assert "STATUS: file" in planner_prompt
    assert "FILE_BYTES:" in planner_prompt
    assert "STATUS: missing" not in planner_prompt.split(f"TASK_SCOPE_PATH: {scoped_file.resolve()}", 1)[1].split("\n\n", 1)[0]


def test_godot_wrapper_uses_static_check_for_headless_project_validation(tmp_path: Path):
    godot_bin = tmp_path / "Godot_v4.6.3-stable_win64_console.exe"
    godot_bin.write_text("fake", encoding="utf-8")
    workspace = tmp_path / "battlefield-workspace"
    settings = get_settings().__class__(godot_bin=str(godot_bin), workspace_dir=workspace)
    runner = CodexExecRunner(settings)
    env = runner._codex_env()
    project = tmp_path / "game"
    (project / "scenes" / "main").mkdir(parents=True)
    (project / "scripts" / "main").mkdir(parents=True)
    (project / "project.godot").write_text('config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n', encoding="utf-8")
    (project / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n\n[ext_resource type="Script" path="res://scripts/main/MainRoot.gd" id="1_main"]\n',
        encoding="utf-8",
    )
    (project / "scripts" / "main" / "MainRoot.gd").write_text("extends Node\n", encoding="utf-8")

    wrapper_script = Path(env["GODOT_BIN"]).with_name("godot_with_timeout.py")
    result = subprocess.run(
        [sys.executable, str(wrapper_script), "--headless", "--path", str(project), "--quit"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "GODOT_STATIC_CHECK ok" in result.stdout


def test_godot_wrapper_uses_static_check_for_quit_after_validation(tmp_path: Path):
    godot_bin = tmp_path / "Godot_v4.6.3-stable_win64_console.exe"
    godot_bin.write_text("fake", encoding="utf-8")
    workspace = tmp_path / "battlefield-workspace"
    settings = get_settings().__class__(godot_bin=str(godot_bin), workspace_dir=workspace)
    runner = CodexExecRunner(settings)
    env = runner._codex_env()
    project = tmp_path / "game"
    _write_minimal_static_godot_project(project)

    wrapper_script = Path(env["GODOT_BIN"]).with_name("godot_with_timeout.py")
    result = subprocess.run(
        [sys.executable, str(wrapper_script), "--headless", "--path", str(project), "--quit-after", "1"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0
    assert "GODOT_STATIC_CHECK ok" in result.stdout
    assert "GODOT_VALIDATION_ENVIRONMENT_ERROR" not in result.stdout


def test_godot_wrapper_cannot_be_bypassed_by_child_env(tmp_path: Path):
    godot_bin = tmp_path / "Godot_v4.6.3-stable_win64_console.exe"
    godot_bin.write_text("fake", encoding="utf-8")
    workspace = tmp_path / "battlefield-workspace"
    settings = get_settings().__class__(godot_bin=str(godot_bin), workspace_dir=workspace)
    runner = CodexExecRunner(settings)
    env = runner._codex_env()
    env["CODEX_BATTLEFIELD_GODOT_STATIC_FALLBACK"] = "0"
    project = tmp_path / "game"
    _write_minimal_static_godot_project(project)

    wrapper_script = Path(env["GODOT_BIN"]).with_name("godot_with_timeout.py")
    result = subprocess.run(
        [sys.executable, str(wrapper_script), "--headless", "--path", str(project), "--check-only"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0
    assert "GODOT_STATIC_CHECK ok" in result.stdout
    assert "CODEX_BATTLEFIELD_REAL_GODOT_BIN is not set" not in result.stdout


def _godot_wrapper_for_test(tmp_path: Path) -> Path:
    godot_bin = tmp_path / "Godot_v4.6.3-stable_win64_console.exe"
    godot_bin.write_text("fake", encoding="utf-8")
    workspace = tmp_path / "battlefield-workspace"
    settings = get_settings().__class__(godot_bin=str(godot_bin), workspace_dir=workspace)
    runner = CodexExecRunner(settings)
    env = runner._codex_env()
    return Path(env["CODEX_BATTLEFIELD_GODOT_WRAPPER_SCRIPT"])


def _write_minimal_static_godot_project(project: Path, main_scene_extra: str = "") -> None:
    (project / "scenes" / "main").mkdir(parents=True)
    (project / "scripts" / "main").mkdir(parents=True)
    (project / "project.godot").write_text(
        'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (project / "scenes" / "main" / "Main.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n\n'
        '[ext_resource type="Script" path="res://scripts/main/MainRoot.gd" id="1_main"]\n'
        f"{main_scene_extra}\n",
        encoding="utf-8",
    )
    (project / "scripts" / "main" / "MainRoot.gd").write_text("extends Node\n", encoding="utf-8")


def test_godot_wrapper_leaf_static_check_allows_future_json_res_reference(tmp_path: Path):
    wrapper_script = _godot_wrapper_for_test(tmp_path)
    project = tmp_path / "future-json-ref-game"
    _write_minimal_static_godot_project(project)
    (project / "data").mkdir(parents=True)
    (project / "data" / "levels.json").write_text(
        dumps({"levels": [{"scene_path": "res://scenes/levels/MissingDock.tscn"}]}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(wrapper_script), "--headless", "--path", str(project), "--quit"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "GODOT_STATIC_CHECK ok (leaf-structural)" in result.stdout


def test_godot_wrapper_strict_static_check_fails_missing_json_res_reference(tmp_path: Path):
    wrapper_script = _godot_wrapper_for_test(tmp_path)
    project = tmp_path / "missing-json-ref-game"
    _write_minimal_static_godot_project(project)
    (project / "data").mkdir(parents=True)
    (project / "data" / "levels.json").write_text(
        dumps({"levels": [{"scene_path": "res://scenes/levels/MissingDock.tscn"}]}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(wrapper_script), "--headless", "--path", str(project), "--quit", "--strict-data-refs"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "missing resource res://scenes/levels/MissingDock.tscn" in result.stdout


def test_godot_wrapper_static_check_accepts_existing_fragment_reference(tmp_path: Path):
    wrapper_script = _godot_wrapper_for_test(tmp_path)
    project = tmp_path / "fragment-ref-game"
    _write_minimal_static_godot_project(project)
    (project / "data" / "spawns").mkdir(parents=True)
    (project / "data" / "spawns" / "arcade_wave_plan.json").write_text(
        dumps({"self_ref": "res://data/spawns/arcade_wave_plan.json#wave_sets/dock"}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(wrapper_script), "--headless", "--path", str(project), "--quit"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "GODOT_STATIC_CHECK ok" in result.stdout


def test_godot_wrapper_static_check_fails_empty_referenced_png(tmp_path: Path):
    wrapper_script = _godot_wrapper_for_test(tmp_path)
    project = tmp_path / "empty-asset-game"
    _write_minimal_static_godot_project(
        project,
        '[ext_resource type="Texture2D" path="res://assets/ui/portrait.png" id="2_portrait"]',
    )
    (project / "assets" / "ui").mkdir(parents=True)
    (project / "assets" / "ui" / "portrait.png").write_bytes(b"")

    result = subprocess.run(
        [sys.executable, str(wrapper_script), "--headless", "--path", str(project), "--quit"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "invalid asset res://assets/ui/portrait.png" in result.stdout


def test_real_runner_defaults_to_codex_cli_model_speed():
    settings = get_settings()

    assert settings.codex_home == ""
    assert settings.codex_profile == ""
    assert settings.codex_model == ""
    assert settings.codex_reasoning_effort == ""


def test_real_runner_uses_noninteractive_approval_policy(tmp_path: Path):
    runner = CodexExecRunner(get_settings().__class__(workspace_dir=tmp_path))
    args = runner._codex_exec_args(tmp_path, tmp_path / "schema.json", tmp_path / "out.json", "read-only")

    assert "--ask-for-approval" in args
    assert args[args.index("--ask-for-approval") + 1] == "never"
    assert args.index("--ask-for-approval") < args.index("exec")


def test_real_runner_isolates_readonly_child_agents_from_user_plugins(tmp_path: Path):
    settings = get_settings()
    runner = CodexExecRunner(settings.__class__(workspace_dir=tmp_path))
    args = runner._codex_exec_args(tmp_path, tmp_path / "schema.json", tmp_path / "out.json", "read-only")

    assert settings.codex_ignore_user_config is True
    assert settings.codex_disable_plugins is True
    assert "--ignore-user-config" in args
    assert "--disable" in args
    assert "plugins" in args
    assert "--ignore-rules" in args
    assert args.index("--disable") < args.index("exec")


def test_real_runner_uses_codex_profile_without_ignoring_user_config(tmp_path: Path):
    settings = get_settings()
    runner = CodexExecRunner(settings.__class__(workspace_dir=tmp_path, codex_profile="battlefield-http"))
    args = runner._codex_exec_args(tmp_path, tmp_path / "schema.json", tmp_path / "out.json", "read-only")

    assert "--profile" in args
    assert args[args.index("--profile") + 1] == "battlefield-http"
    assert args.index("--profile") < args.index("exec")
    assert "--ignore-user-config" not in args
    assert "--disable" in args
    assert "plugins" in args
    assert "--ignore-rules" in args


def test_real_runner_keeps_workspace_write_children_writable(tmp_path: Path):
    settings = get_settings()
    runner = CodexExecRunner(settings.__class__(workspace_dir=tmp_path))
    args = runner._codex_exec_args(tmp_path, tmp_path / "schema.json", tmp_path / "out.json", "workspace-write")

    assert settings.codex_ignore_user_config is True
    assert "--sandbox" in args
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert "--ignore-user-config" not in args
    assert "--disable" in args
    assert "plugins" in args
    assert "--ignore-rules" in args


def test_real_runner_marks_codex_usage_errors_as_tool_internal():
    runner = CodexExecRunner(get_settings())
    payload = runner._failure_payload("planner", "Codex planner exited with code 2.\nerror: unexpected argument '--ask-for-approval' found\nUsage: codex exec [OPTIONS]")

    assert runner._is_tool_internal_failure(payload["reason"])
    payload = runner._attach_tool_internal_block_report(payload, "planner", 3)
    assert payload["block_report"]["category"] == "tool_internal"
    assert payload["block_report"]["parent_action"] == "pause_tool_fix"
    assert payload["block_report"]["retryable"] is False


def test_acceptance_repair_cycle_limit_defaults_to_two():
    """默认 **2**（2026-09-22 决策，`docs/spec-modularization.md` §6 D1）。

    以前默认 0 = "不限"，那等于把铁律 8（父节点多轮 repair 进人工审查）默认关掉。
    `0` 仍然是"不限"这个语义本身，只是不再当默认值——要用旧行为就显式设 0
    （`CODEX_BATTLEFIELD_MAX_ACCEPTANCE_REPAIR_CYCLES=0`）。
    """
    settings = get_settings()
    assert settings.max_acceptance_repair_cycles == 2
    assert Orchestrator(settings)._acceptance_repair_cycle_limit() == 2
    unlimited = Orchestrator(settings.__class__(max_acceptance_repair_cycles=0))
    assert unlimited._acceptance_repair_cycle_limit() == 0


def test_real_runner_detects_transient_capacity_errors():
    runner = CodexExecRunner(get_settings())

    assert runner.settings.codex_timeout_seconds == 0
    assert runner.settings.codex_idle_timeout_seconds == 1800
    assert runner._is_transient_failure("429 rate limit: model overloaded, try again later")
    assert runner._is_transient_failure("Codex worker timed out after 300s")
    assert runner._is_transient_failure("Codex worker idle watchdog fired after 1800s without stdout or output-file activity.")
    assert runner._is_transient_failure('rmcp::transport::worker: worker quit with fatal: Transport channel closed, when UnexpectedContentType(Some("missing-content-type; body: "))')
    assert runner._is_transient_failure("服务繁忙，请稍后重试")
    assert not runner._is_transient_failure("schema validation failed: required field is missing")


def test_real_runner_extracts_quota_retry_window_from_codex_output():
    runner = CodexExecRunner(get_settings().__class__(resource_block_resume_after_seconds=18_000))

    assert runner._retry_after_seconds_from_message("429 rate limit, retry after 5 hours") == 18_000
    assert runner._retry_after_seconds_from_message("账户额度不足，请等待 300 分钟后重试") == 18_000
    assert runner._retry_after_seconds_from_message("quota reset window: 5 hours") == 18_000
    assert runner._retry_after_seconds_from_message("Retry-After: 120") == 120
    future = datetime.now().astimezone() + timedelta(minutes=30)
    clock_message = f"You've hit your usage limit. Visit settings or try again at {future.strftime('%I:%M %p')}."
    clock_retry = runner._retry_after_seconds_from_message(clock_message)
    assert clock_retry is not None and 60 <= clock_retry <= 3600
    observed = datetime.now(timezone.utc) - timedelta(minutes=60)
    expired_reset = datetime.now().astimezone() - timedelta(minutes=5)
    expired_message = (
        f"You've hit your usage limit at {observed.isoformat().replace('+00:00', 'Z')}. "
        f"Visit settings or try again at {expired_reset.strftime('%I:%M %p')}."
    )
    assert runner._retry_after_seconds_from_message(expired_message) == 60
    reconnect_delay = "stream disconnected - retrying sampling request (2/5 in 429ms); request timed out"
    assert runner._retry_after_seconds_from_message(reconnect_delay) is None
    assert runner._transient_failure_category(reconnect_delay) == "timeout"
    assert runner._is_transient_failure("HTTP status 429: too many requests")
    assert runner._transient_failure_category("HTTP status 429: too many requests") == "rate_limit"

    payload = runner._attach_retryable_block_report({"summary": "429 rate limit, retry after 5 hours"}, "worker", 3)
    report = payload["block_report"]
    assert report["category"] == "rate_limit"
    assert report["retryable"] is True
    assert report["retry_after_seconds"] == 18_000
    assert report["quota_window_seconds"] == 18_000
    assert report["retry_after_source"] == "codex_output"

    transport_payload = runner._attach_retryable_block_report(
        {"summary": 'rmcp::transport::worker: worker quit with fatal: Transport channel closed, when UnexpectedContentType(Some("missing-content-type; body: "))'},
        "worker",
        2,
    )
    assert transport_payload["block_report"]["category"] == "transport"
    assert transport_payload["block_report"]["retryable"] is True


def test_orchestrator_reclassifies_legacy_transport_tool_internal_block_report():
    orchestrator = Orchestrator(get_settings())
    reason = '工具内部错误，已阻止递归拆分并等待工具修复：rmcp::transport::worker: worker quit with fatal: Transport channel closed, when UnexpectedContentType(Some("missing-content-type; body: "))'
    legacy_report = {
        "task_id": "node-legacy-transport",
        "title": "Legacy transport blocker",
        "status": "blocked",
        "source": "worker.blocked",
        "category": "tool_internal",
        "retryable": False,
        "parent_action": "pause_tool_fix",
        "reason": reason,
    }
    task = TaskNode(
        id="node-legacy-transport",
        title="Legacy transport blocker",
        description="",
        depth=1,
        status=TaskStatus.BLOCKED.value,
        node_meta_json=dumps({"block_report": legacy_report}),
    )

    report = orchestrator._child_block_report(task)

    assert report["category"] == "transport"
    assert report["retryable"] is True
    assert report["parent_action"] == "wait_and_retry"
    assert report["reclassified_from"]["category"] == "tool_internal"
    assert orchestrator._dependency_reports_are_retryable({"dependency_reports": [legacy_report]})
    assert orchestrator._dependency_report_context(legacy_report) == {
        "category": "transport",
        "retryable": True,
        "parent_action": "wait_and_retry",
    }
    assert orchestrator._block_category(reason) == "transport"


class WorkerSelfDecomposeRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "planner",
            PlannerDecision(
                decision="execute",
                reason="测试规划器认为节点可以交给 worker 先自检。",
                leaf_certainty="300%",
            ).model_dump(),
        )

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            return envelope(
                "worker",
                WorkerResult(
                    status="needs_decomposition",
                    summary="worker 自检发现自己不能 300% 完成根节点，必须转父节点。",
                    suggested_children=[
                        ChildTaskSpec(title="实现收敛子任务", description="缩小实现范围。", acceptance=["实现证据存在。"]),
                        ChildTaskSpec(title="验证收敛子任务", description="补齐验证证据。", acceptance=["验证证据存在。"]),
                    ],
                ).model_dump(),
            )
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary="子任务完成。",
                changed_files=[f"{task.id}.txt"],
                evidence=["子任务证据"],
            ).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope(
            "verifier",
            VerifierResult(verdict="pass", reason="子任务验证通过。", evidence=worker_result.evidence).model_dump(),
        )

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope(
            "acceptance",
            AcceptanceResult(
                verdict="pass",
                reason="父节点整合验收通过。",
                evidence=[f"已整合 {len(children)} 个子节点"],
                constitution_verdict="within_bounds",
            ).model_dump(),
        )


class WorkerFailedDecomposeRunner(WorkerSelfDecomposeRunner):
    async def work(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            return envelope(
                "worker",
                WorkerResult(
                    status="failed",
                    summary="Worker discovered the leaf cannot be accepted as-is and must split.",
                    evidence=["global resource integrity failed"],
                    suggested_children=[
                        ChildTaskSpec(
                            title="Repair failed leaf deliverable",
                            description="Repair the failed leaf deliverable recursively.",
                            acceptance=["Repair evidence exists."],
                        )
                    ],
                ).model_dump(),
            )
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary="Repair child completed.",
                changed_files=[f"{task.id}.txt"],
                evidence=["repair child evidence"],
            ).model_dump(),
        )


class WorkerEmptyExecutableDecomposeThenSuccessRunner(WorkerSelfDecomposeRunner):
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        calls = self.calls.get(task.id, 0) + 1
        self.calls[task.id] = calls
        if calls == 1:
            return envelope(
                "worker",
                WorkerResult(
                    status="needs_decomposition",
                    summary="This leaf has a clear boundary and can complete directly in the current worktree; I will generate and validate it.",
                    suggested_children=[],
                ).model_dump(),
            )
        if task.worktree_path:
            path = Path(task.worktree_path) / "retry-success.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("retry succeeded\n", encoding="utf-8")
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary="Retry completed the leaf.",
                changed_files=["retry-success.txt"],
                tests=["retry-success.txt exists"],
                evidence=["worker retried after empty decomposition"],
            ).model_dump(),
        )


class FoundationMissingBlockRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        return envelope("planner", PlannerDecision(decision="execute", reason="Try as a leaf.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            return envelope(
                "worker",
                WorkerResult(
                    status="blocked",
                    summary="工作目录为空，未发现 README、package/project 配置、入口文件或资源目录。",
                    evidence=["empty worktree audit"],
                ).model_dump(),
            )
        return envelope(
            "worker",
            WorkerResult(status="succeeded", summary="Child resolved foundation work.", changed_files=[f"{task.id}.txt"], evidence=["ok"]).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


class AcceptanceRepairOnceRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.repaired = False

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        if task.depth == 0:
            payload = PlannerDecision(
                decision="decompose",
                reason="根节点需要两个子任务。",
                children=[
                    ChildTaskSpec(title="子任务 A", description="完成 A。", acceptance=["A 通过。"]),
                    ChildTaskSpec(title="子任务 B", description="完成 B。", acceptance=["B 通过。"]),
                ],
            ).model_dump()
        else:
            payload = PlannerDecision(
                decision="execute",
                reason="叶子足够小。",
                leaf_certainty="300%",
            ).model_dump()
        return envelope("planner", payload)

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        return envelope(
            "worker",
            WorkerResult(
                status="succeeded",
                summary=f"{task.title} 完成。",
                changed_files=[f"{task.id}.txt"],
                evidence=[f"{task.title} 证据"],
            ).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="验证通过。").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        if task.depth == 0 and not self.repaired:
            self.repaired = True
            return envelope(
                "acceptance",
                AcceptanceResult(
                    verdict="repair",
                    reason="父节点整合验收发现缺少粘合任务。",
                    suggested_children=[
                        ChildTaskSpec(title="整合修复子任务", description="补齐 A/B 整合。", acceptance=["整合证据存在。"]),
                    ],
                    constitution_verdict="within_bounds",
                ).model_dump(),
            )
        return envelope(
            "acceptance",
            AcceptanceResult(
                verdict="pass",
                reason="父节点二次整合验收通过。",
                constitution_verdict="within_bounds",
            ).model_dump(),
        )


class MissingChangedFilesRepairRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        return envelope("planner", PlannerDecision(decision="execute", reason="Leaf looks small enough.", leaf_certainty="300%").model_dump())

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        rel = "deliverable.txt"
        if task.parent_id and task.worktree_path:
            path = Path(task.worktree_path) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("real repaired deliverable\n", encoding="utf-8")
        payload = WorkerResult(
            status="succeeded",
            summary="Worker reports a deliverable.",
            changed_files=[rel],
            evidence=["worker reported changed_files"],
        ).model_dump()
        return RunnerEnvelope(
            command="test worker with real workdir",
            exit_code=0,
            logs_path=f"logs/{task.id}-worker.jsonl",
            prompt_path=f"prompts/{task.id}-worker.txt",
            last_message_path=f"last/{task.id}-worker.json",
            workdir=task.worktree_path,
            payload=payload,
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


@pytest.mark.asyncio
async def test_worker_needs_decomposition_converts_leaf_to_parent_and_records_debug_paths():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test worker self decomposition.")
    root_id = await orchestrator.start_project("Test worker self decomposition.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, WorkerSelfDecomposeRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        detail = orchestrator.get_detail(db, root_id)
        child_count = db.query(TaskNode).filter(TaskNode.parent_id == root_id).count()

    assert root.status == TaskStatus.DONE.value
    assert root.converted_from_leaf is True
    assert child_count == 2
    assert detail["runs"]
    assert all(run["prompt_path"] for run in detail["runs"])
    assert all(run["last_message_path"] for run in detail["runs"])
    assert all(run["workdir"] for run in detail["runs"])
    assert all(run["constitution_hash"] == root.constitution_hash for run in detail["runs"])


@pytest.mark.asyncio
async def test_worker_failed_converts_leaf_to_parent_instead_of_terminal_failure():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test worker failed self recovery.")
    root_id = await orchestrator.start_project("Test worker failed self recovery.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, WorkerFailedDecomposeRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        children = db.query(TaskNode).filter(TaskNode.parent_id == root_id).order_by(TaskNode.created_at).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == root_id).all()

    assert root.status == TaskStatus.DONE.value
    assert root.converted_from_leaf is True
    assert len(children) == 1
    assert children[0].title == "Repair failed leaf deliverable"
    assert children[0].status == TaskStatus.DONE.value
    assert any(event.type == "worker.failed_converted" for event in events)


@pytest.mark.asyncio
async def test_empty_executable_worker_decomposition_requeues_once_then_succeeds(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    runner = WorkerEmptyExecutableDecomposeThenSuccessRunner()

    constitution_id = await locked_constitution(orchestrator, "Test empty self-decomposition retry.")
    root_id = await orchestrator.start_project("Test empty self-decomposition retry.", constitution_id=constitution_id, auto_run=False)

    await orchestrator._process_node(root_id, runner, asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        first_meta = loads(root.node_meta_json, {})
        first_child_count = db.query(TaskNode).filter(TaskNode.parent_id == root_id).count()
        first_events = db.query(EventRecord).filter(EventRecord.task_id == root_id).all()

    assert root.status == TaskStatus.QUEUED.value
    assert root.converted_from_leaf is False
    assert root.leaf_attempts == 1
    assert first_child_count == 0
    assert first_meta["worker_empty_decomposition_retry_count"] == 1
    assert any(event.type == "worker.empty_decomposition_retry" for event in first_events)

    await orchestrator._process_node(root_id, runner, asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        final_child_count = db.query(TaskNode).filter(TaskNode.parent_id == root_id).count()

    assert root.status == TaskStatus.DONE.value
    assert root.converted_from_leaf is False
    assert root.leaf_attempts == 2
    assert final_child_count == 0


def test_recover_empty_executable_worker_last_message_requeues_instead_of_blocking(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    task_id = "recover-empty-executable-worker"
    run_dir = settings.workspace_dir / "runs" / task_id
    run_dir.mkdir(parents=True)
    worker_result = WorkerResult(
        status="needs_decomposition",
        summary="This leaf boundary is clear and can complete directly in the current worktree.",
        suggested_children=[],
    )
    last_message_path = run_dir / "worker-last-message.json"
    last_message_path.write_text(worker_result.model_dump_json(), encoding="utf-8")

    with SessionLocal() as db:
        task = TaskNode(
            id=task_id,
            title="Recover empty executable worker",
            description="Worker last-message has contradictory empty decomposition.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            worktree_path=str(tmp_path / "worktree"),
        )
        db.add(task)
        db.commit()

        recovered = orchestrator._recover_finished_agent_messages(db, [task], min_age_seconds=0)
        db.commit()
        db.refresh(task)
        children = db.query(TaskNode).filter(TaskNode.parent_id == task.id).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == task.id).all()
        meta = loads(task.node_meta_json, {})

    assert recovered == 1
    assert task.status == TaskStatus.QUEUED.value
    assert task.converted_from_leaf is False
    assert children == []
    assert meta["worker_empty_decomposition_retry_count"] == 1
    assert any(event.type == "worker.empty_decomposition_retry" for event in events)


@pytest.mark.asyncio
async def test_missing_worker_changed_files_converts_leaf_to_recursive_repair():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test missing worker changed_files repair.")
    root_id = await orchestrator.start_project("Test missing worker changed_files repair.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, MissingChangedFilesRepairRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        children = db.query(TaskNode).filter(TaskNode.parent_id == root_id).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "worker.changed_files_missing").all()
        detail = orchestrator.get_detail(db, root_id)

    assert root.status == TaskStatus.DONE.value
    assert root.converted_from_leaf is True
    assert len(children) == 1
    assert "changed_files" in children[0].title
    assert events
    assert detail["task"]["result"]["accepted_children"] == [children[0].id]
    assert root.worktree_path
    assert (Path(root.worktree_path) / "deliverable.txt").is_file()


@pytest.mark.asyncio
async def test_foundation_missing_worker_block_converts_leaf_to_parent():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    goal = "Create a runnable startup bootstrap project from an empty target."
    constitution_id = await locked_constitution(orchestrator, goal)
    root_id = await orchestrator.start_project(goal, constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, FoundationMissingBlockRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        children = db.query(TaskNode).filter(TaskNode.parent_id == root_id).order_by(TaskNode.created_at).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "worker.foundation_missing").all()

    assert root.status == TaskStatus.DONE.value
    assert root.converted_from_leaf is True
    assert len(children) == 2
    assert "创建最小工程骨架" in children[0].title
    assert "补齐启动说明" in children[1].title
    assert events


@pytest.mark.asyncio
async def test_parent_acceptance_repair_adds_child_then_passes():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        machine_safety_min_available_memory_mb=0,
    )
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test parent acceptance repair.")
    root_id = await orchestrator.start_project("Test parent acceptance repair.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, AcceptanceRepairOnceRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        children = db.query(TaskNode).filter(TaskNode.parent_id == root_id).all()
        acceptance_runs = [run for run in root.runs if run.kind == RunKind.ACCEPTANCE.value]

    assert root.status == TaskStatus.DONE.value
    assert len(children) == 3
    assert len(acceptance_runs) == 2


def test_recover_finished_acceptance_last_message_creates_repair_child(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    run_dir = settings.workspace_dir / "runs" / "stuck-acceptance-parent"
    run_dir.mkdir(parents=True)
    repair_result = AcceptanceResult(
        verdict="repair",
        reason="Recovered repair result.",
        suggested_children=[
            ChildTaskSpec(
                title="Repair recovered evidence",
                description="Repair the evidence mismatch.",
                acceptance=["Recovered child is created."],
                coverage_ids=["acceptance-recovery"],
                estimated_leaf=True,
            )
        ],
    )
    last_message_path = run_dir / "acceptance-last-message.json"
    last_message_path.write_text(repair_result.model_dump_json(), encoding="utf-8")
    (run_dir / "acceptance.jsonl").write_text('{"type":"turn.completed"}\n', encoding="utf-8")

    with SessionLocal() as db:
        parent = TaskNode(
            id="stuck-acceptance-parent",
            title="Stuck acceptance parent",
            description="Acceptance completed on disk but was not recorded.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            acceptance_json=dumps(["Parent acceptance."]),
            worktree_path=str(tmp_path / "integrated"),
        )
        done_child = TaskNode(
            id="stuck-acceptance-done-child",
            parent_id=parent.id,
            title="Done child",
            description="Already done.",
            depth=1,
            status=TaskStatus.DONE.value,
        )
        db.add_all([parent, done_child])
        db.commit()

        recovered = orchestrator._recover_finished_acceptance_messages(db, [parent], min_age_seconds=0)
        db.commit()
        db.refresh(parent)
        children = db.query(TaskNode).filter(TaskNode.parent_id == parent.id).order_by(TaskNode.created_at).all()
        runs = db.query(RunRecord).filter(RunRecord.task_id == parent.id, RunRecord.kind == RunKind.ACCEPTANCE.value).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == parent.id).all()
        meta = loads(parent.node_meta_json, {})

    assert recovered == 1
    assert parent.status == TaskStatus.WAITING_CHILDREN.value
    assert len(children) == 2
    assert children[-1].title == "Repair recovered evidence"
    assert runs[0].last_message_path == str(last_message_path)
    assert any(event.type == "acceptance.last_message_recovered" for event in events)
    assert meta["recovered_acceptance_last_message_sha"]


def test_recover_finished_acceptance_last_message_settles_already_recovered_state(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    run_dir = settings.workspace_dir / "runs" / "already-recovered-parent"
    run_dir.mkdir(parents=True)
    repair_result = AcceptanceResult(
        verdict="repair",
        reason="Repair child has already completed.",
        suggested_children=[
            ChildTaskSpec(
                title="Refresh recovered evidence",
                description="Refresh the stale evidence.",
                acceptance=["Evidence is refreshed."],
                coverage_ids=["acceptance-recovery"],
                estimated_leaf=True,
            )
        ],
    )
    last_message_path = run_dir / "acceptance-last-message.json"
    raw = repair_result.model_dump_json().encode("utf-8")
    last_message_path.write_bytes(raw)

    with SessionLocal() as db:
        parent = TaskNode(
            id="already-recovered-parent",
            title="Already recovered parent",
            description="Acceptance recovery was recorded but status stayed active.",
            depth=0,
            status=TaskStatus.ACCEPTING.value,
            node_meta_json=dumps({"recovered_acceptance_last_message_sha": hashlib.sha1(raw).hexdigest()}),
        )
        original_child = TaskNode(
            id="already-recovered-original-child",
            parent_id=parent.id,
            title="Original child",
            description="Already done.",
            depth=1,
            status=TaskStatus.DONE.value,
        )
        repair_child = TaskNode(
            id="already-recovered-repair-child",
            parent_id=parent.id,
            title="Refresh recovered evidence",
            description="Already repaired.",
            depth=1,
            status=TaskStatus.DONE.value,
        )
        db.add_all([parent, original_child, repair_child])
        db.commit()

        recovered = orchestrator._recover_finished_acceptance_messages(db, [parent], min_age_seconds=0)
        db.commit()
        db.refresh(parent)
        children = db.query(TaskNode).filter(TaskNode.parent_id == parent.id).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == parent.id).all()

    assert recovered == 1
    assert parent.status == TaskStatus.QUEUED.value
    assert len(children) == 2
    assert any(event.type == "acceptance.recovered_state_settled" for event in events)


class RecoveredPlannerExecuteRunner(BaseCodexRunner):
    def __init__(self) -> None:
        self.plan_calls = 0
        self.work_calls = 0

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        self.plan_calls += 1
        raise AssertionError("planner should not run after recovered execute decision")

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        self.work_calls += 1
        return envelope(
            "worker",
            WorkerResult(status="succeeded", summary="Recovered planner went straight to worker.", tests=["worker ran"]).model_dump(),
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        return envelope("verifier", VerifierResult(verdict="pass", reason="Recovered worker verified.").model_dump())

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return envelope("acceptance", AcceptanceResult(verdict="pass", reason="Accepted.").model_dump())


@pytest.mark.asyncio
async def test_recover_finished_planner_execute_last_message_skips_replanning(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    task_id = "stuck-planner-leaf"
    run_dir = settings.workspace_dir / "runs" / task_id
    run_dir.mkdir(parents=True)
    planner_result = PlannerDecision(decision="execute", reason="Recovered executable leaf.", leaf_certainty="300%")
    last_message_path = run_dir / "planner-last-message.json"
    last_message_path.write_text(planner_result.model_dump_json(), encoding="utf-8")
    runner = RecoveredPlannerExecuteRunner()

    with SessionLocal() as db:
        parent = TaskNode(id="planner-recovery-parent", title="Parent", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        task = TaskNode(
            id=task_id,
            parent_id=parent.id,
            title="Stuck planner leaf",
            description="Planner finished on disk but task state stayed active.",
            depth=1,
            status=TaskStatus.PLANNING.value,
            worktree_path=str(tmp_path / "worktree"),
        )
        db.add_all([parent, task])
        db.commit()

        recovered = orchestrator._recover_finished_agent_messages(db, [task], min_age_seconds=0)
        db.commit()
        db.refresh(task)
        pending_decision = orchestrator._pending_recovered_planner_decision(db, task)

    assert recovered == 1
    assert task.status == TaskStatus.QUEUED.value
    assert pending_decision is not None and pending_decision.decision == "execute"

    await orchestrator._process_node(task_id, runner, asyncio.Semaphore(1))

    with SessionLocal() as db:
        task = db.get(TaskNode, task_id)
        assert task is not None
        planner_runs = db.query(RunRecord).filter(RunRecord.task_id == task_id, RunRecord.kind == RunKind.PLANNER.value).all()
        worker_runs = db.query(RunRecord).filter(RunRecord.task_id == task_id, RunRecord.kind == RunKind.WORKER.value).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == task_id).all()

    assert runner.plan_calls == 0
    assert runner.work_calls == 1
    assert task.status == TaskStatus.DONE.value
    assert len(planner_runs) == 1
    assert planner_runs[0].last_message_path == str(last_message_path)
    assert len(worker_runs) == 1
    assert any(event.type == "planner.last_message_recovered" for event in events)


def test_recover_finished_planner_last_message_waits_when_process_scan_failed(monkeypatch, tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    task_id = "scan-failed-active-planner"
    run_dir = settings.workspace_dir / "runs" / task_id
    run_dir.mkdir(parents=True)
    (run_dir / "planner-last-message.json").write_text(
        PlannerDecision(decision="execute", reason="Would be recovered if scan were healthy.", leaf_certainty="300%").model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_external_codex_processes", lambda max_cache_age_seconds=0.5: [])
    orchestrator._external_codex_process_scan_failed = True

    with SessionLocal() as db:
        task = TaskNode(
            id=task_id,
            title="Scan failed active planner",
            description="Active state must be preserved until process scan succeeds.",
            depth=1,
            status=TaskStatus.PLANNING.value,
        )
        db.add(task)
        db.commit()

        recovered = orchestrator._recover_finished_agent_messages(db, [task], min_age_seconds=0)
        db.commit()
        db.refresh(task)

    assert recovered == 0
    assert task.status == TaskStatus.PLANNING.value


def test_recover_finished_worker_last_message_queues_verifier(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    run_dir = settings.workspace_dir / "runs" / "stuck-worker-leaf"
    run_dir.mkdir(parents=True)
    worker_result = WorkerResult(
        status="succeeded",
        summary="Recovered worker result.",
        changed_files=["docs/recovered_worker.json"],
        tests=["json ok"],
        evidence=["last-message exists"],
    )
    last_message_path = run_dir / "worker-last-message.json"
    last_message_path.write_text(worker_result.model_dump_json(), encoding="utf-8")
    (run_dir / "worker.jsonl").write_text('{"type":"turn.completed"}\n', encoding="utf-8")

    with SessionLocal() as db:
        task = TaskNode(
            id="stuck-worker-leaf",
            title="Stuck worker leaf",
            description="Worker finished on disk but was not recorded.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            worktree_path=str(tmp_path / "worktree"),
        )
        db.add(task)
        db.commit()

        recovered = orchestrator._recover_finished_agent_messages(db, [task], min_age_seconds=0)
        db.commit()
        db.refresh(task)
        runs = db.query(RunRecord).filter(RunRecord.task_id == task.id, RunRecord.kind == RunKind.WORKER.value).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == task.id).all()

    assert recovered == 1
    assert task.status == TaskStatus.QUEUED.value
    assert orchestrator._pending_worker_result(task) is not None
    assert runs[0].last_message_path == str(last_message_path)
    assert any(event.type == "worker.last_message_recovered" for event in events)


@pytest.mark.parametrize("recoverable_status", [TaskStatus.WORKING.value, TaskStatus.QUEUED.value])
def test_recover_active_or_queued_worker_last_message_without_external_process_ignores_age(
    tmp_path: Path, recoverable_status: str
):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    task_id = f"orphan-worker-leaf-{recoverable_status}"
    run_dir = settings.workspace_dir / "runs" / task_id
    run_dir.mkdir(parents=True)
    worker_result = WorkerResult(
        status="needs_decomposition",
        summary="Recovered orphan worker result should split immediately.",
        suggested_children=[
            ChildTaskSpec(
                title="Recovered child",
                description="Continue after orphan worker.",
                scope=["docs"],
                acceptance=["child exists"],
                coverage_ids=["orphan-recovery"],
                estimated_leaf=True,
            )
        ],
    )
    last_message_path = run_dir / "worker-last-message.json"
    last_message_path.write_text(worker_result.model_dump_json(), encoding="utf-8")

    with SessionLocal() as db:
        task = TaskNode(
            id=task_id,
            title="Orphan worker leaf",
            description="Worker last-message exists but active task has no external Codex process.",
            depth=1,
            status=recoverable_status,
            worktree_path=str(tmp_path / "worktree"),
        )
        db.add(task)
        db.commit()

        recovered = orchestrator._recover_finished_agent_messages(db, [task], min_age_seconds=999_999)
        db.commit()
        db.refresh(task)
        children = db.query(TaskNode).filter(TaskNode.parent_id == task.id).all()
        runs = db.query(RunRecord).filter(RunRecord.task_id == task.id, RunRecord.kind == RunKind.WORKER.value).all()

    assert recovered == 1
    assert task.status == TaskStatus.WAITING_CHILDREN.value
    assert [child.title for child in children] == ["Recovered child"]
    assert runs[0].last_message_path == str(last_message_path)


def test_recover_finished_verifier_last_message_marks_leaf_done(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    run_dir = settings.workspace_dir / "runs" / "stuck-verifier-leaf"
    run_dir.mkdir(parents=True)
    worker_result = WorkerResult(
        status="succeeded",
        summary="Worker already recovered.",
        changed_files=["docs/recovered_worker.json"],
        tests=["json ok"],
        evidence=["worker evidence"],
    )
    verifier_result = VerifierResult(
        verdict="pass",
        reason="Recovered verifier passed.",
        checks=["checked file"],
        evidence=["verifier last-message exists"],
        constitution_verdict="within_bounds",
    )
    last_message_path = run_dir / "verifier-last-message.json"
    last_message_path.write_text(verifier_result.model_dump_json(), encoding="utf-8")
    (run_dir / "verifier.jsonl").write_text('{"type":"turn.completed"}\n', encoding="utf-8")

    with SessionLocal() as db:
        task = TaskNode(
            id="stuck-verifier-leaf",
            title="Stuck verifier leaf",
            description="Verifier finished on disk but was not recorded.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            result_json=dumps({"worker": worker_result.model_dump()}),
            worktree_path=str(tmp_path / "worktree"),
        )
        db.add(task)
        db.commit()

        recovered = orchestrator._recover_finished_agent_messages(db, [task], min_age_seconds=0)
        db.commit()
        db.refresh(task)
        runs = db.query(RunRecord).filter(RunRecord.task_id == task.id, RunRecord.kind == RunKind.VERIFIER.value).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == task.id).all()

    assert recovered == 1
    assert task.status == TaskStatus.DONE.value
    assert runs[0].last_message_path == str(last_message_path)
    assert any(event.type == "verifier.last_message_recovered" for event in events)


def test_recover_existing_verifier_run_settles_active_state(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    task_id = "already-recorded-verifier-leaf"
    run_dir = settings.workspace_dir / "runs" / task_id
    run_dir.mkdir(parents=True)
    worker_result = WorkerResult(status="succeeded", summary="Worker already recorded.", tests=["worker ok"])
    verifier_result = VerifierResult(
        verdict="pass",
        reason="Verifier result was already recorded but state stayed active.",
        checks=["checked"],
        evidence=["existing verifier run"],
    )
    last_message_path = run_dir / "verifier-last-message.json"
    last_message_path.write_text(verifier_result.model_dump_json(), encoding="utf-8")

    with SessionLocal() as db:
        task = TaskNode(
            id=task_id,
            title="Already recorded verifier leaf",
            description="Verifier run exists but task status is still active.",
            depth=1,
            status=TaskStatus.VERIFYING.value,
            result_json=dumps({"worker": worker_result.model_dump()}),
            worktree_path=str(tmp_path / "worktree"),
        )
        run = RunRecord(
            task_id=task_id,
            kind=RunKind.VERIFIER.value,
            status=RunStatus.SUCCEEDED.value,
            command="existing verifier",
            sandbox="workspace-write",
            exit_code=0,
            result_json=verifier_result.model_dump_json(),
            last_message_path=str(last_message_path),
            workdir=str(tmp_path / "worktree"),
        )
        db.add_all([task, run])
        db.commit()

        recovered = orchestrator._recover_finished_agent_messages(db, [task], min_age_seconds=0)
        db.commit()
        db.refresh(task)
        events = db.query(EventRecord).filter(EventRecord.task_id == task.id).all()

    assert recovered == 1
    assert task.status == TaskStatus.DONE.value
    assert any(event.type == "verifier.recovered_state_settled" for event in events)


def test_repeated_child_signature_creates_cycle_breaker_child():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        max_repeated_child_signature=2,
    )
    orchestrator = Orchestrator(settings)
    spec = ChildTaskSpec(title="Same split", description="Repeat work.", acceptance=["Repeat acceptance."])
    signature = orchestrator._child_signature(spec)

    with SessionLocal() as db:
        parent = TaskNode(id="parent-repeat", title="Parent", description="Parent", depth=0, status=TaskStatus.PLANNED.value)
        first = TaskNode(
            id="child-repeat-1",
            parent_id=parent.id,
            title="Same split",
            description="Repeat work.",
            depth=1,
            status=TaskStatus.DONE.value,
            node_meta_json=dumps({"split_signature": signature}),
        )
        second = TaskNode(
            id="child-repeat-2",
            parent_id=parent.id,
            title="Same split",
            description="Repeat work.",
            depth=1,
            status=TaskStatus.DONE.value,
            node_meta_json=dumps({"split_signature": signature}),
        )
        db.add_all([parent, first, second])
        db.commit()

        child_ids = orchestrator._create_children(db, parent, [spec])
        db.commit()
        db.refresh(parent)
        created = [db.get(TaskNode, child_id) for child_id in child_ids]
        converted_events = db.query(EventRecord).filter(EventRecord.type == "task.repeated_split_converted").all()
        meta = loads(parent.node_meta_json, {})

    assert len(child_ids) == 1
    assert created[0] is not None
    assert "重复拆分收敛破环" in created[0].title
    assert parent.status == TaskStatus.PLANNED.value
    assert signature in meta["cycle_breaker_signatures"]
    assert converted_events


def test_repeated_child_signature_stops_after_cycle_breaker_attempt():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        max_repeated_child_signature=2,
    )
    orchestrator = Orchestrator(settings)
    spec = ChildTaskSpec(title="Same split", description="Repeat work.", acceptance=["Repeat acceptance."])
    signature = orchestrator._child_signature(spec)

    with SessionLocal() as db:
        parent = TaskNode(
            id="parent-repeat-after-breaker",
            title="Parent",
            description="Parent",
            depth=0,
            status=TaskStatus.PLANNED.value,
            node_meta_json=dumps({"cycle_breaker_signatures": [signature]}),
        )
        first = TaskNode(
            id="child-repeat-a",
            parent_id=parent.id,
            title="Same split",
            description="Repeat work.",
            depth=1,
            status=TaskStatus.DONE.value,
            node_meta_json=dumps({"split_signature": signature}),
        )
        second = TaskNode(
            id="child-repeat-b",
            parent_id=parent.id,
            title="Same split",
            description="Repeat work.",
            depth=1,
            status=TaskStatus.DONE.value,
            node_meta_json=dumps({"split_signature": signature}),
        )
        db.add_all([parent, first, second])
        db.commit()

        child_ids = orchestrator._create_children(db, parent, [spec])
        db.commit()
        db.refresh(parent)
        manual_events = db.query(EventRecord).filter(EventRecord.type == "task.manual_review_required").all()

    assert child_ids == []
    assert parent.status == TaskStatus.REPAIR.value
    assert manual_events


def test_convergence_slice_loop_creates_cycle_breaker_child():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        parent_skill_note_task_enabled=False,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(
            id="convergence-loop-parent",
            title="重新生成报告：验证收敛切片：验证收敛切片",
            description="Parent is already looping.",
            depth=0,
            status=TaskStatus.PLANNED.value,
        )
        child = ChildTaskSpec(
            title="重新生成报告：验证收敛切片：验证收敛切片：验证收敛切片",
            description="This would add another fallback layer.",
            acceptance=["Do not keep looping."],
        )
        db.add(parent)
        db.commit()

        child_ids = orchestrator._create_children(db, parent, [child])
        db.commit()
        db.refresh(parent)
        created = [db.get(TaskNode, child_id) for child_id in child_ids]
        events = db.query(EventRecord).filter(EventRecord.task_id == parent.id).all()
        meta = loads(parent.node_meta_json, {})

    assert len(child_ids) == 1
    assert created[0] is not None
    assert "递归循环诊断与收口" in created[0].title
    assert meta["convergence_slice_cycle_breaker_attempted"] is True
    assert any(event.type == "task.convergence_loop_converted" for event in events)


def test_convergence_cycle_breaker_parent_allows_diagnostic_children():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        parent_skill_note_task_enabled=False,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(
            id="convergence-breaker-parent",
            title="重新生成并对齐 Player.tscn 报告：验证收敛切片：实现收敛切片：验证收敛切片：递归循环诊断与收口",
            description="Already a cycle breaker; its children are diagnostic closure tasks.",
            depth=3,
            status=TaskStatus.PLANNED.value,
            node_meta_json=dumps(
                {
                    "coverage_ids": ["cycle-breaker", "convergence-slice-loop"],
                    "verification_kind": "convergence loop breaker + parent closure evidence",
                }
            ),
        )
        children = [
            ChildTaskSpec(title="定位递归循环根因与权威输入状态", description="Find root cause."),
            ChildTaskSpec(title="生成父层可执行收口建议或最小修复包", description="Create closure package."),
            ChildTaskSpec(title="归并本轮递归经验 Skill 记录", description="Merge skill notes."),
        ]
        db.add(parent)
        db.commit()

        child_ids = orchestrator._create_children(db, parent, children)
        db.commit()
        created = [db.get(TaskNode, child_id) for child_id in child_ids]
        events = db.query(EventRecord).filter(EventRecord.task_id == parent.id).all()

    assert len(child_ids) == 3
    assert [child.title for child in created if child is not None] == [child.title for child in children]
    assert all("递归循环诊断与收口" not in child.title for child in created if child is not None)
    assert not any(event.type == "task.convergence_loop_converted" for event in events)


def test_convergence_cycle_breaker_title_normalizes_repeated_suffix():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    parent = TaskNode(
        id="convergence-breaker-title",
        title="重新生成报告：验证收敛切片：验证收敛切片：验证收敛切片：递归循环诊断与收口：递归循环诊断与收口",
        description="Parent title already has repeated breaker suffix.",
        depth=0,
        status=TaskStatus.PLANNED.value,
    )

    child = orchestrator._convergence_cycle_breaker_children(parent, {"limit": 3})[0]

    assert child.title.endswith("：递归循环诊断与收口")
    assert child.title.count("递归循环诊断与收口") == 1


def test_redundant_convergence_cycle_breaker_child_is_pruned_before_launch():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(
            id="convergence-breaker-prune-parent",
            title="重新生成报告：验证收敛切片：验证收敛切片：验证收敛切片：递归循环诊断与收口",
            description="Parent is already the convergence cycle breaker.",
            depth=2,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps(
                {
                    "coverage_ids": ["cycle-breaker", "convergence-slice-loop"],
                    "verification_kind": "convergence loop breaker + parent closure evidence",
                }
            ),
        )
        redundant = TaskNode(
            id="redundant-cycle-breaker-child",
            parent_id=parent.id,
            title="重新生成报告：验证收敛切片：验证收敛切片：验证收敛切片：递归循环诊断与收口：递归循环诊断与收口",
            description="Redundant queued breaker.",
            depth=3,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps(
                {
                    "coverage_ids": ["cycle-breaker", "convergence-slice-loop"],
                    "verification_kind": "convergence loop breaker + parent closure evidence",
                }
            ),
        )
        normal = TaskNode(
            id="normal-diagnostic-child",
            parent_id=parent.id,
            title="定位递归循环根因与权威输入状态",
            description="Normal diagnostic task should still run.",
            depth=3,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([parent, redundant, normal])
        db.commit()

        siblings = list(db.query(TaskNode).filter(TaskNode.parent_id == parent.id).all())
        settled_ids = orchestrator._settle_redundant_convergence_cycle_breaker_children(db, parent, siblings)
        db.commit()
        redundant = db.get(TaskNode, "redundant-cycle-breaker-child")
        normal = db.get(TaskNode, "normal-diagnostic-child")
        events = db.query(EventRecord).filter(EventRecord.task_id == parent.id).all()

    assert settled_ids == {"redundant-cycle-breaker-child"}
    assert redundant is not None and redundant.status == TaskStatus.DONE.value
    assert normal is not None and normal.status == TaskStatus.QUEUED.value
    assert loads(redundant.result_json, {})["system"]["status"] == "superseded_by_parent_convergence_cycle_breaker"
    assert any(event.type == "scheduler.redundant_convergence_cycle_breaker_pruned" for event in events)


def test_legacy_convergence_slice_queue_is_pruned_before_launch():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        parent_skill_note_task_enabled=False,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(
            id="legacy-loop-parent",
            title="重新生成并对齐父集成目录 Player.tscn 解析报告",
            description="Parent has stale queued fallback children.",
            depth=9,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        first = TaskNode(
            id="legacy-loop-a",
            parent_id=parent.id,
            title="重新生成报告：验证收敛切片：验证收敛切片：验证收敛切片",
            description="Old fallback slice.",
            depth=10,
            status=TaskStatus.QUEUED.value,
        )
        second = TaskNode(
            id="legacy-loop-b",
            parent_id=parent.id,
            title="重新生成报告：实现收敛切片：验证收敛切片：实现收敛切片",
            description="Another old fallback slice.",
            depth=10,
            status=TaskStatus.QUEUED.value,
        )
        normal = TaskNode(
            id="normal-child",
            parent_id=parent.id,
            title="真实收口证据",
            description="Still needs normal processing.",
            depth=10,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([parent, first, second, normal])
        db.commit()

        siblings = list(db.query(TaskNode).filter(TaskNode.parent_id == parent.id).all())
        settled_ids, breaker_ids = orchestrator._settle_legacy_convergence_slice_children(db, parent, siblings)
        db.commit()

        first = db.get(TaskNode, "legacy-loop-a")
        second = db.get(TaskNode, "legacy-loop-b")
        normal = db.get(TaskNode, "normal-child")
        breaker = db.get(TaskNode, next(iter(breaker_ids)))
        parent_events = db.query(EventRecord).filter(EventRecord.task_id == parent.id).all()

    assert settled_ids == {"legacy-loop-a", "legacy-loop-b"}
    assert len(breaker_ids) == 1
    assert first is not None and first.status == TaskStatus.DONE.value
    assert second is not None and second.status == TaskStatus.DONE.value
    assert normal is not None and normal.status == TaskStatus.QUEUED.value
    assert breaker is not None and breaker.status == TaskStatus.QUEUED.value
    breaker_meta = loads(breaker.node_meta_json, {})
    assert "convergence-slice-loop" in breaker_meta["coverage_ids"]
    assert loads(first.result_json, {})["system"]["breaker_child_id"] == breaker.id
    assert any(event.type == "scheduler.legacy_convergence_slice_pruned" for event in parent_events)


@pytest.mark.asyncio
async def test_acceptance_repair_cycle_limit_requires_manual_review():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        max_acceptance_repair_cycles=1,
    )
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test acceptance repair cap.")
    root_id = await orchestrator.start_project("Test acceptance repair cap.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, AcceptanceAlwaysRepairRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = dumps(root.node_meta_json)
        acceptance_runs = [run for run in root.runs if run.kind == RunKind.ACCEPTANCE.value]
        manual_events = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "task.manual_review_required").all()

    assert root.status == TaskStatus.REPAIR.value
    assert len(acceptance_runs) == 1
    assert "acceptance_repair_cycle_limit" in meta
    assert manual_events


@pytest.mark.asyncio
async def test_concurrent_same_node_run_is_skipped():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    runner = SlowPlanningRunner()

    constitution_id = await locked_constitution(orchestrator, "Test concurrent node guard.")
    root_id = await orchestrator.start_project("Test concurrent node guard.", constitution_id=constitution_id, auto_run=False)
    first = asyncio.create_task(orchestrator._process_node(root_id, runner, asyncio.Semaphore(1)))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(orchestrator._process_node(root_id, runner, asyncio.Semaphore(1)))
    await asyncio.gather(first, second)

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        planner_runs = [run for run in root.runs if run.kind == RunKind.PLANNER.value]
        skipped = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "task.concurrent_skip").all()

    assert root.status == TaskStatus.DONE.value
    assert runner.plan_calls == 1
    assert len(planner_runs) == 1
    assert skipped


@pytest.mark.asyncio
async def test_existing_children_resume_without_replanning_parent():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    constitution_id = await locked_constitution(orchestrator, "Resume an existing tree.")
    root_id = await orchestrator.start_project("Resume an existing tree.", constitution_id=constitution_id, auto_run=False)

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.status = TaskStatus.WAITING_CHILDREN.value
        child = TaskNode(
            id="resume-child",
            parent_id=root.id,
            title="Existing queued child",
            description="Continue this child instead of replanning the parent.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            acceptance_json=dumps(["Child is done."]),
            constitution_id=root.constitution_id,
            constitution_hash=root.constitution_hash,
            constitution_json=root.constitution_json,
        )
        db.add(child)
        db.commit()

    runner = ExistingChildResumeRunner()
    await orchestrator._process_node(root_id, runner, asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        child = db.get(TaskNode, "resume-child")

    assert root is not None
    assert child is not None
    assert runner.planned_task_ids == ["resume-child"]
    assert child.status == TaskStatus.DONE.value
    assert root.status == TaskStatus.DONE.value


@pytest.mark.asyncio
async def test_retryable_child_block_is_reported_to_parent_without_blocking_parent():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir, auto_hibernate_on_resource_block=False)
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test retryable child block report.")
    root_id = await orchestrator.start_project("Test retryable child block report.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, ResourceBlockedChildRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        child = db.query(TaskNode).filter(TaskNode.parent_id == root_id).one()
        root_meta = loads(root.node_meta_json, {})
        child_meta = loads(child.node_meta_json, {})
        report_events = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "parent.child_block_reports").all()

    assert root.status == TaskStatus.WAITING_CHILDREN.value
    assert child.status == TaskStatus.BLOCKED.value
    assert root_meta["blocked_child_reports"][0]["parent_action"] == "wait_and_retry"
    assert child_meta["block_report"]["retryable"] is True
    assert child_meta["block_report"]["parent_action"] == "wait_and_retry"
    assert report_events


def test_dependency_block_preserves_retryable_resource_report():
    orchestrator = Orchestrator(get_settings())
    task = TaskNode(id="dependent", title="Dependent", description="", depth=1, status=TaskStatus.QUEUED.value)

    report = orchestrator._build_block_report(
        task,
        "Dependency failed because child hit a resource limit.",
        "dependency.blocked",
        {
            "dependencies": ["resource-child"],
            "dependency_reports": [
                {
                    "task_id": "resource-child",
                    "title": "Resource child",
                    "status": TaskStatus.BLOCKED.value,
                    "source": "verifier.blocked",
                    "reason": "429 rate limit, retry after 5 hours",
                    "retryable": True,
                    "parent_action": "wait_and_retry",
                }
            ],
        },
    )

    assert report["category"] == "rate_limit"
    assert report["retryable"] is True
    assert report["parent_action"] == "wait_and_retry"


def test_dependency_block_recovers_retryable_resource_reason_when_flag_was_lost():
    orchestrator = Orchestrator(get_settings())
    task = TaskNode(id="dependent", title="Dependent", description="", depth=1, status=TaskStatus.QUEUED.value)

    report = orchestrator._build_block_report(
        task,
        "Dependency failed because an intermediate node lost retryable metadata.",
        "dependency.blocked",
        {
            "dependencies": ["intermediate-child"],
            "dependency_reports": [
                {
                    "task_id": "intermediate-child",
                    "title": "Intermediate child",
                    "status": TaskStatus.BLOCKED.value,
                    "source": "dependency.blocked",
                    "reason": "资源/流量类阻塞，可等待后重试：Codex verifier exited with code 1.",
                    "retryable": False,
                    "parent_action": "skill_lookup_recovery",
                }
            ],
        },
    )

    assert report["category"] == "rate_limit"
    assert report["retryable"] is True
    assert report["parent_action"] == "wait_and_retry"


def test_retryable_resource_reason_beats_node_id_401_and_plugin_noise():
    orchestrator = Orchestrator(get_settings())
    task = TaskNode(id="dependent", title="Dependent", description="", depth=1, status=TaskStatus.QUEUED.value)
    reason = (
        "依赖节点 node-401034ff09 未成功完成。根因：资源/流量类阻塞，可等待后重试："
        "Codex verifier exited with code 1. plugin openai-curated emitted a non-blocking warning."
    )

    report = orchestrator._build_block_report(task, reason, "dependency.blocked", None)

    assert report["category"] == "rate_limit"
    assert report["retryable"] is True
    assert report["parent_action"] == "wait_and_retry"


def test_godot_resource_queue_timeout_is_retryable_capacity_block():
    orchestrator = Orchestrator(get_settings())
    task = TaskNode(id="godot-queue", title="Godot queue", description="", depth=1, status=TaskStatus.QUEUED.value)

    report = orchestrator._build_block_report(
        task,
        "GODOT_RESOURCE_QUEUE_TIMEOUT after 1800s waiting for global Godot lock.",
        "verifier.blocked",
        None,
    )

    assert report["retryable"] is True
    assert report["parent_action"] == "wait_and_retry"
    assert report["category"] in {"capacity", "rate_limit", "timeout", "transient"}


@pytest.mark.asyncio
async def test_retryable_child_block_auto_hibernates_tree_for_later_resume():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        auto_hibernate_on_resource_block=True,
        resource_block_resume_after_seconds=60,
    )
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test retryable child auto hibernate.")
    root_id = await orchestrator.start_project("Test retryable child auto hibernate.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, ResourceBlockedChildRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        child = db.query(TaskNode).filter(TaskNode.parent_id == root_id).one()
        root_meta = loads(root.node_meta_json, {})
        child_meta = loads(child.node_meta_json, {})

    assert root.status == TaskStatus.HIBERNATING.value
    assert child.status == TaskStatus.HIBERNATING.value
    assert root_meta["hibernation"]["owner_id"] == root_id
    assert child_meta["hibernation"]["previous_status"] == TaskStatus.BLOCKED.value
    assert child_meta["block_report"]["retryable"] is True
    assert root_id in orchestrator._auto_resume_tasks

    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)


@pytest.mark.asyncio
async def test_agent_burst_limit_hibernates_before_starting_next_codex_agent():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        agent_burst_limit=1,
        agent_burst_resume_after_seconds=60,
    )
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test token reserve budget guard.")
    root_id = await orchestrator.start_project("Test token reserve budget guard.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, ResourceBlockedChildRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        child = db.query(TaskNode).filter(TaskNode.parent_id == root_id).one()
        guard_events = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "budget.guard_hibernate").all()
        child_runs = db.query(RunRecord).filter(RunRecord.task_id == child.id).all()
        tree = orchestrator.get_tree(db, root_id)
        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    assert root.status == TaskStatus.HIBERNATING.value
    assert child.status == TaskStatus.HIBERNATING.value
    assert orchestrator._agent_starts_by_root[root_id] == 1
    assert root_id in orchestrator._auto_resume_tasks
    assert guard_events
    assert child_runs == []
    assert tree["stats"]["agent_burst_used"] == 1
    assert tree["stats"]["agent_burst_limit"] == 1
    assert state["budget_warning_level"] == "paused"
    assert "工作启动额度已用完" in state["budget_warning_message"]

    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)


@pytest.mark.asyncio
async def test_agent_burst_limit_is_ignored_in_goal_mode():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        goal_mode=True,
        agent_burst_limit=1,
        agent_burst_resume_after_seconds=60,
    )
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test goal mode long-running budget.")
    root_id = await orchestrator.start_project("Test goal mode long-running budget.", constitution_id=constitution_id, auto_run=False)

    assert await orchestrator._reserve_agent_start(root_id, "planner") is True
    assert await orchestrator._reserve_agent_start(root_id, "worker") is True

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        tree = orchestrator.get_tree(db, root_id)

    assert root.status != TaskStatus.HIBERNATING.value
    assert root_id not in orchestrator._auto_resume_tasks
    assert tree["stats"]["goal_mode"] == 1
    assert tree["stats"]["agent_burst_limit"] == 0
    assert tree["stats"]["agent_burst_work_limit"] == 0
    assert tree["stats"]["agent_burst_remaining"] == 0

    with SessionLocal() as db:
        state = orchestrator.get_watchdog_state(db, root_id=root_id)

    assert state["goal_mode"] is True
    assert state["budget_warning_level"] == "goal"
    assert "Goal 长跑模式已开启" in state["budget_warning_message"]


@pytest.mark.asyncio
async def test_machine_safety_guard_hibernates_before_starting_codex_agent(monkeypatch):
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        machine_safety_min_available_memory_mb=4096,
        machine_safety_resume_after_seconds=60,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_available_memory_mb", lambda: 512.0)

    root = TaskNode(id="machine-safety-root", title="Root", description="Protect the machine.", depth=0, status=TaskStatus.QUEUED.value)
    with SessionLocal() as db:
        db.add(root)
        db.commit()

    allowed = await orchestrator._reserve_agent_start(root.id, "planner")

    with SessionLocal() as db:
        stored = db.get(TaskNode, root.id)
        events = db.query(EventRecord).filter(EventRecord.task_id == root.id, EventRecord.type == "machine_safety.guard_hibernate").all()

    assert allowed is False
    assert stored is not None
    assert stored.status == TaskStatus.HIBERNATING.value
    assert events
    assert root.id in orchestrator._auto_resume_tasks

    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)


def test_watchdog_state_resumes_machine_safety_hibernation_when_memory_recovers(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        machine_safety_min_available_memory_mb=4096,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_available_memory_mb", lambda: 8192.0)
    orchestrator._codex_slots_active = lambda *args, **kwargs: 0
    resume_at = datetime.now(timezone.utc) + timedelta(hours=1)
    root = TaskNode(
        id="machine-safety-recovered-root",
        title="Root",
        description="Recover early.",
        depth=0,
        status=TaskStatus.HIBERNATING.value,
        node_meta_json=dumps(
            {
                "hibernation": {
                    "owner_id": "machine-safety-recovered-root",
                    "previous_status": TaskStatus.QUEUED.value,
                    "resume_at": resume_at.isoformat(),
                    "reason": "Machine safety guard: available memory is below the configured threshold.",
                    "status": "scheduled",
                }
            }
        ),
    )
    with SessionLocal() as db:
        db.add(root)
        db.commit()
        state = orchestrator.get_watchdog_state(db, root_id=root.id)

    assert state["machine_safety_recovered"] is True
    assert state["resume_due"] is True
    assert state["should_resume"] is True
    assert state["reason"].startswith("Machine safety guard recovered")


def test_watchdog_state_holds_machine_safety_hibernation_while_memory_low(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        machine_safety_min_available_memory_mb=4096,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_available_memory_mb", lambda: 1024.0)
    orchestrator._codex_slots_active = lambda *args, **kwargs: 0
    resume_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    root = TaskNode(
        id="machine-safety-still-low-root",
        title="Root",
        description="Do not resume while memory is still low.",
        depth=0,
        status=TaskStatus.HIBERNATING.value,
        node_meta_json=dumps(
            {
                "hibernation": {
                    "owner_id": "machine-safety-still-low-root",
                    "previous_status": TaskStatus.QUEUED.value,
                    "resume_at": resume_at.isoformat(),
                    "reason": "Machine safety guard: available memory is below the configured threshold.",
                    "status": "scheduled",
                }
            }
        ),
    )
    with SessionLocal() as db:
        db.add(root)
        db.commit()
        state = orchestrator.get_watchdog_state(db, root_id=root.id)

    assert state["resume_due"] is False
    assert state["should_resume"] is False
    assert state["recommended_action"] == "machine_safety_sleep"
    assert state["guardian_mode"] == "protect_machine"
    assert "Machine safety guard is still active" in state["reason"]


def test_watchdog_state_repairs_resumed_hibernation_meta_for_sleeping_task(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        machine_safety_min_available_memory_mb=0,
    )
    orchestrator = Orchestrator(settings)
    orchestrator._codex_slots_active = lambda *args, **kwargs: 0
    resume_at = datetime.now(timezone.utc) + timedelta(hours=1)
    root = TaskNode(
        id="hibernation-meta-half-resumed-root",
        title="Root",
        description="Repair metadata mismatch.",
        depth=0,
        status=TaskStatus.HIBERNATING.value,
        node_meta_json=dumps(
            {
                "hibernation": {
                    "owner_id": "hibernation-meta-half-resumed-root",
                    "previous_status": TaskStatus.QUEUED.value,
                    "resume_at": resume_at.isoformat(),
                    "reason": "wait for token refresh",
                    "status": "resumed",
                    "resumed_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        ),
    )
    with SessionLocal() as db:
        db.add(root)
        db.commit()
        state = orchestrator.get_watchdog_state(db, root_id=root.id)
        stored = db.get(TaskNode, root.id)
        assert stored is not None
        meta = loads(stored.node_meta_json, {})
        event = (
            db.query(EventRecord)
            .filter(EventRecord.task_id == root.id, EventRecord.type == "watchdog.hibernation_meta_repaired")
            .one()
        )

    assert state["resume_due"] is False
    assert state["recommended_action"] == "wait_for_resume_at"
    assert meta["hibernation"]["status"] == "scheduled"
    assert meta["hibernation"]["status_repaired_at"]
    assert loads(event.payload_json, {})["repaired_count"] == 1


def test_watchdog_state_clears_machine_safety_budget_guard_when_memory_recovers(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        machine_safety_min_available_memory_mb=4096,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_available_memory_mb", lambda: 8192.0)
    orchestrator._codex_slots_active = lambda *args, **kwargs: 0
    resume_at = datetime.now(timezone.utc) + timedelta(hours=1)
    root = TaskNode(
        id="machine-safety-budget-guard-root",
        title="Root",
        description="Recover early with budget guard.",
        depth=0,
        status=TaskStatus.HIBERNATING.value,
        node_meta_json=dumps(
            {
                "hibernation": {
                    "owner_id": "machine-safety-budget-guard-root",
                    "previous_status": TaskStatus.QUEUED.value,
                    "resume_at": resume_at.isoformat(),
                    "reason": "Machine safety guard: available memory is below the configured threshold.",
                    "status": "scheduled",
                }
            }
        ),
    )
    orchestrator._write_budget_guard(
        {
            "status": "active",
            "root_id": root.id,
            "resume_at": resume_at.isoformat(),
            "reason": "Machine safety guard: available_memory_mb=1024 below safe_min=4096.",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resume_after_seconds": 1800,
        }
    )

    with SessionLocal() as db:
        db.add(root)
        db.commit()
        state = orchestrator.get_watchdog_state(db, root_id=root.id)

    guard_payload = orchestrator._read_budget_guard()
    assert state["budget_guard_active"] is False
    assert state["machine_safety_recovered"] is True
    assert state["resume_due"] is True
    assert state["should_resume"] is True
    assert state["recommended_action"] == "resume"
    assert guard_payload["status"] == "cleared"
    assert guard_payload["cleared_for_root_id"] == root.id


@pytest.mark.asyncio
async def test_agent_burst_reserve_hibernates_before_reserved_start():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        agent_burst_limit=3,
        agent_burst_reserve=1,
        agent_burst_resume_after_seconds=60,
    )
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test automation token reserve.")
    root_id = await orchestrator.start_project("Test automation token reserve.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, ResourceBlockedChildRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        guard_event = db.query(EventRecord).filter(EventRecord.task_id == root_id, EventRecord.type == "budget.guard_hibernate").one()
        guard_payload = loads(guard_event.payload_json, {})
        tree = orchestrator.get_tree(db, root_id)

    assert root.status == TaskStatus.HIBERNATING.value
    assert orchestrator._agent_starts_by_root[root_id] == 2
    assert guard_payload["limit"] == 3
    assert guard_payload["work_limit"] == 2
    assert guard_payload["reserve"] == 1
    assert tree["stats"]["agent_burst_used"] == 2
    assert tree["stats"]["agent_burst_limit"] == 3
    assert tree["stats"]["agent_burst_work_limit"] == 2
    assert tree["stats"]["agent_burst_reserve"] == 1
    assert tree["stats"]["agent_burst_remaining"] == 0

    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)


@pytest.mark.asyncio
async def test_agent_burst_usage_survives_orchestrator_restart():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        agent_burst_limit=3,
        agent_burst_reserve=1,
    )
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test persistent burst accounting.")
    root_id = await orchestrator.start_project("Test persistent burst accounting.", constitution_id=constitution_id, auto_run=False)

    first_reserved = await orchestrator._reserve_agent_start(root_id, "planner")
    assert first_reserved is True

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        meta.pop("agent_burst_used", None)
        root.node_meta_json = dumps(meta)
        db.commit()

    reconstructed = Orchestrator(settings)
    with SessionLocal() as db:
        tree = reconstructed.get_tree(db, root_id)

    assert tree["stats"]["agent_burst_used"] == 1
    assert reconstructed._agent_starts_by_root[root_id] == 1

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        meta["agent_burst_used"] = 1
        root.node_meta_json = dumps(meta)
        db.commit()

    restarted = Orchestrator(settings)
    with SessionLocal() as db:
        tree = restarted.get_tree(db, root_id)
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})

    assert tree["stats"]["agent_burst_used"] == 1
    assert meta["agent_burst_used"] == 1
    assert restarted._agent_starts_by_root[root_id] == 1

    second_reserved = await restarted._reserve_agent_start(root_id, "worker")
    assert second_reserved is True

    with SessionLocal() as db:
        tree = restarted.get_tree(db, root_id)
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})

    assert tree["stats"]["agent_burst_used"] == 2
    assert meta["agent_burst_used"] == 2
    assert restarted._agent_starts_by_root[root_id] == 2


@pytest.mark.asyncio
async def test_canceled_node_ignores_late_planner_result():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)
    runner = SlowPlanningRunner()

    constitution_id = await locked_constitution(orchestrator, "Test late cancel guard.")
    root_id = await orchestrator.start_project("Test late cancel guard.", constitution_id=constitution_id, auto_run=False)
    task = asyncio.create_task(orchestrator._process_node(root_id, runner, asyncio.Semaphore(1)))
    await asyncio.sleep(0.01)
    canceled = orchestrator.cancel_tree(root_id)
    await task

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        planner_runs = [run for run in root.runs if run.kind == RunKind.PLANNER.value]

    assert canceled == 1
    assert root.status == TaskStatus.CANCELED.value
    assert planner_runs == []


@pytest.mark.asyncio
async def test_child_dependencies_are_respected_before_execution():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir, max_concurrency=2)
    orchestrator = Orchestrator(settings)
    runner = DependencyOrderRunner()

    constitution_id = await locked_constitution(orchestrator, "Test dependency scheduling.")
    root_id = await orchestrator.start_project("Test dependency scheduling.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, runner, asyncio.Semaphore(2))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        children = db.query(TaskNode).filter(TaskNode.parent_id == root_id).all()
        feature = next(child for child in children if child.title == "Feature")
        feature_meta = loads(feature.node_meta_json, {})

    assert root.status == TaskStatus.DONE.value
    assert runner.work_order[:2] == ["Base", "Feature"]
    assert feature_meta["depends_on"] == ["Base"]
    assert feature_meta["depends_on_ids"]


@pytest.mark.asyncio
async def test_dependent_planner_sees_done_dependency_changed_files(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace", max_concurrency=2)
    orchestrator = Orchestrator(settings)
    runner = PlannerDependencyContextRunner()

    constitution_id = await locked_constitution(orchestrator, "Test dependency context before planning.")
    root_id = await orchestrator.start_project("Test dependency context before planning.", constitution_id=constitution_id, auto_run=False)
    await orchestrator._process_node(root_id, runner, asyncio.Semaphore(2))

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        feature = db.query(TaskNode).filter(TaskNode.parent_id == root_id, TaskNode.title == "Feature").one()
        feature_artifact = Path(feature.worktree_path or "") / "docs" / "source_inventory.json"

    assert root is not None and root.status == TaskStatus.DONE.value
    assert runner.feature_planner_saw_dependency is True
    assert feature_artifact.is_file()


@pytest.mark.asyncio
async def test_leaf_worker_and_verifier_keep_same_execution_slot():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    runner = SequentialLeafRunner()

    with SessionLocal() as db:
        root = TaskNode(id="root", parent_id=None, title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        child_a = TaskNode(id="child-a", parent_id="root", title="Child A", description="Do A.", depth=1)
        child_b = TaskNode(id="child-b", parent_id="root", title="Child B", description="Do B.", depth=1)
        db.add_all([root, child_a, child_b])
        db.commit()

    await orchestrator._process_children_with_dependencies("root", ["child-a", "child-b"], runner, asyncio.Semaphore(1))

    phases = [event.split(":", 1)[0] for event in runner.events]
    assert phases == ["work", "verify", "work", "verify"]
    assert runner.events[1] == f"verify:{runner.events[0].split(':', 1)[1]}"
    assert runner.events[3] == f"verify:{runner.events[2].split(':', 1)[1]}"

    with SessionLocal() as db:
        children = db.query(TaskNode).filter(TaskNode.parent_id == "root").all()

    assert {child.status for child in children} == {TaskStatus.DONE.value}


@pytest.mark.asyncio
async def test_run_tree_uses_global_agent_semaphore():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    seen: list[asyncio.Semaphore] = []

    async def fake_process_node(task_id: str, runner: BaseCodexRunner, semaphore: asyncio.Semaphore) -> None:
        seen.append(semaphore)

    orchestrator._process_node = fake_process_node  # type: ignore[method-assign]

    await asyncio.gather(orchestrator.run_tree("root-a", "mock"), orchestrator.run_tree("root-b", "mock"))

    assert seen == [orchestrator._agent_semaphore, orchestrator._agent_semaphore]


@pytest.mark.asyncio
async def test_auto_concurrency_scales_up_when_frontier_is_large(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=1,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=3,
        auto_concurrency_queue_threshold=3,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        children = [
            TaskNode(id=f"auto-concurrency-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(4)
        ]
        db.add_all([root, *children])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-root")
        events = db.query(EventRecord).filter(EventRecord.task_id == "auto-concurrency-root", EventRecord.type == "concurrency.auto_adjust").all()

    assert orchestrator._agent_semaphore.capacity == 3
    assert tree["stats"]["max_concurrency"] == 3
    assert tree["stats"]["auto_concurrency_mode"] == "high_throughput"
    assert events


@pytest.mark.asyncio
async def test_auto_concurrency_opens_thirteen_slots_for_saturated_frontier(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        auto_concurrency_queue_threshold=6,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-thirteen-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        children = [
            TaskNode(id=f"auto-concurrency-thirteen-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(30)
        ]
        db.add_all([root, *children])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-thirteen-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-thirteen-root")

    assert orchestrator._agent_semaphore.capacity == 13
    assert tree["stats"]["auto_concurrency_mode"] == "high_throughput"


def test_auto_concurrency_fuel_uses_real_slots_not_stale_statuses(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        auto_concurrency_queue_threshold=6,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_codex_slots_active", lambda *args, **kwargs: 5)

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-stale-status-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        active = [
            TaskNode(id=f"auto-concurrency-stale-active-{index}", parent_id=root.id, title=f"Active {index}", description="", depth=1, status=TaskStatus.WORKING.value)
            for index in range(14)
        ]
        queued = [
            TaskNode(id=f"auto-concurrency-stale-queued-{index}", parent_id=root.id, title=f"Queued {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(20)
        ]
        db.add_all([root, *active, *queued])
        db.commit()

        decision = orchestrator._auto_concurrency_decision(db, root.id)

    assert decision["active_agent_statuses"] == 14
    assert decision["codex_slots_active"] == 5
    assert decision["slot_pressure_agents"] == 5
    assert decision["target"] == 13
    assert decision["fuel_frontier_pressure"] == 1.0


def test_auto_concurrency_fuel_score_drives_midrange_target(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(orchestrator_module, "utcnow", lambda: datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        auto_concurrency_queue_threshold=6,
        auto_concurrency_fuel_deadline_hour=20,
        auto_concurrency_fuel_timezone_offset_hours=0,
        auto_concurrency_fuel_sprint_window_minutes=120,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-fuel-score-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        queued = [
            TaskNode(id=f"auto-concurrency-fuel-score-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(5)
        ]
        db.add_all([root, *queued])
        db.commit()

        decision = orchestrator._auto_concurrency_decision(db, root.id)

    assert decision["target"] == 3
    assert decision["desired"] == 3
    assert decision["fuel_recommended_target"] == 3
    assert decision["fuel_open_frontier"] == 5
    assert decision["fuel_time_pressure"] == 0.0
    assert decision["fuel_score"] > 0


def test_watchdog_topoff_recommendation_uses_real_slots_not_stale_statuses(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        auto_concurrency_queue_threshold=6,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_codex_slots_active", lambda *args, **kwargs: 5)

    with SessionLocal() as db:
        root = TaskNode(id="watchdog-stale-status-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        active = [
            TaskNode(id=f"watchdog-stale-active-{index}", parent_id=root.id, title=f"Active {index}", description="", depth=1, status=TaskStatus.WORKING.value)
            for index in range(14)
        ]
        queued = [
            TaskNode(id=f"watchdog-stale-queued-{index}", parent_id=root.id, title=f"Queued {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(20)
        ]
        db.add_all([root, *active, *queued])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root.id)

    assert state["active_agent_statuses"] == 14
    assert state["codex_slots_active"] == 5
    assert state["active_frontier"] == 34
    assert state["recommended_action"] == "top_off"


def test_watchdog_goal_mode_suppresses_topoff_during_machine_pressure(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        goal_mode=True,
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        machine_safety_min_available_memory_mb=4096,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(orchestrator, "_available_memory_mb", lambda: 1024.0)
    monkeypatch.setattr(orchestrator, "_codex_slots_active", lambda *args, **kwargs: 0)

    with SessionLocal() as db:
        root = TaskNode(
            id="watchdog-machine-pressure-root",
            title="Root",
            description="Parent.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        queued = [
            TaskNode(
                id=f"watchdog-machine-pressure-queued-{index}",
                parent_id=root.id,
                title=f"Queued {index}",
                description="",
                depth=1,
                status=TaskStatus.QUEUED.value,
            )
            for index in range(20)
        ]
        db.add_all([root, *queued])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root.id)

    assert state["recommended_action"] == "machine_safety_sleep"
    assert state["guardian_mode"] == "protect_machine"
    assert state["l4_fuel_mode"] == "machine_safety_cooldown"
    assert state["l4_fuel_target"] == 1


def test_status_reports_l4_target_capacity_before_limiter_adjustment(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        goal_mode=False,
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        auto_concurrency_queue_threshold=6,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="auto-capacity-report-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        children = [
            TaskNode(id=f"auto-capacity-report-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(30)
        ]
        db.add_all([root, *children])
        db.commit()

        tree = orchestrator.get_tree(db, root_id=root.id)
        state = orchestrator.get_watchdog_state(db, root_id=root.id)
        snapshot_path = orchestrator._write_state_snapshot(db, root.id, "test.capacity", keep_history=False)

    assert orchestrator._agent_semaphore.capacity == 1
    assert tree["stats"]["auto_concurrency_target"] == 13
    assert tree["stats"]["codex_slots_capacity"] == 13
    assert tree["stats"]["max_concurrency"] == 13
    assert state["l4_fuel_target"] == 13
    assert state["codex_slots_capacity"] == 13
    assert snapshot_path is not None
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["stats"]["codex_slots_capacity"] == 13


@pytest.mark.asyncio
async def test_auto_concurrency_sprints_after_fuel_deadline_when_queue_remains(tmp_path: Path):
    now = datetime.now(timezone.utc)
    deadline_hour = now.hour if now.minute > 0 else (now.hour - 1) % 24
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        goal_mode=True,
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        auto_concurrency_queue_threshold=6,
        auto_concurrency_fuel_deadline_hour=deadline_hour,
        auto_concurrency_fuel_timezone_offset_hours=0,
    )
    orchestrator = Orchestrator(settings)
    await orchestrator._agent_semaphore.set_capacity(1)

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-after-deadline-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        children = [
            TaskNode(id=f"auto-concurrency-after-deadline-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(10)
        ]
        db.add_all([root, *children])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-after-deadline-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-after-deadline-root")

    assert orchestrator._agent_semaphore.capacity == 13
    assert tree["stats"]["auto_concurrency_mode"] == "utilization_sprint"
    assert tree["stats"]["auto_concurrency_fuel_basis"] == "operator_utilization_deadline_elapsed"
    assert "避免夜间额度闲置" in tree["stats"]["auto_concurrency_reason"]


def test_goal_mode_auto_concurrency_starts_at_max_capacity(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        goal_mode=True,
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
    )
    orchestrator = Orchestrator(settings)

    assert orchestrator._agent_semaphore.capacity == 13


@pytest.mark.asyncio
async def test_auto_concurrency_scales_down_on_retryable_resource_pressure(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=3,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=3,
        auto_concurrency_queue_threshold=3,
    )
    orchestrator = Orchestrator(settings)
    await orchestrator._agent_semaphore.set_capacity(3)

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-pressure-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        queued = [
            TaskNode(id=f"auto-concurrency-pressure-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(4)
        ]
        blocked = TaskNode(
            id="auto-concurrency-pressure-blocked",
            parent_id=root.id,
            title="Rate limited child",
            description="Blocked by capacity.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            node_meta_json=dumps({"block_report": {"retryable": True, "category": "rate_limit", "reason": "429 rate limit"}}),
        )
        db.add_all([root, *queued, blocked])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-pressure-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-pressure-root")
        events = db.query(EventRecord).filter(EventRecord.task_id == "auto-concurrency-pressure-root", EventRecord.type == "concurrency.auto_adjust").all()

    assert orchestrator._agent_semaphore.capacity == 1
    assert tree["stats"]["auto_concurrency_mode"] == "resource_cooldown"
    assert tree["stats"]["auto_concurrency_resource_pressure"] == 1
    assert events


@pytest.mark.asyncio
async def test_auto_concurrency_does_not_scale_down_on_transport_retryable_block(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=3,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=3,
        auto_concurrency_queue_threshold=3,
    )
    orchestrator = Orchestrator(settings)
    await orchestrator._agent_semaphore.set_capacity(3)
    reason = 'rmcp::transport::worker: worker quit with fatal: Transport channel closed, when UnexpectedContentType(Some("missing-content-type; body: "))'

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-transport-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        queued = [
            TaskNode(id=f"auto-concurrency-transport-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(4)
        ]
        blocked = TaskNode(
            id="auto-concurrency-transport-blocked",
            parent_id=root.id,
            title="Transport child",
            description="Blocked by transport.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "retryable": False,
                        "category": "tool_internal",
                        "parent_action": "pause_tool_fix",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, *queued, blocked])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-transport-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-transport-root")

    assert orchestrator._agent_semaphore.capacity == 3
    assert tree["stats"]["auto_concurrency_mode"] != "resource_cooldown"
    assert tree["stats"]["auto_concurrency_resource_pressure"] == 0


def test_transport_block_is_fast_requeued_without_resource_hibernation():
    orchestrator = Orchestrator(get_settings())
    reason = 'rmcp::transport::worker: worker quit with fatal: Transport channel closed, when UnexpectedContentType(Some("missing-content-type; body: "))'

    with SessionLocal() as db:
        root = TaskNode(id="fast-retry-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="fast-retry-transport-child",
            parent_id=root.id,
            title="Transport child",
            description="Blocked by transport.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "fast-retry-transport-child",
                        "title": "Transport child",
                        "status": "blocked",
                        "retryable": False,
                        "category": "tool_internal",
                        "parent_action": "pause_tool_fix",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("fast-retry-root") == 1

    with SessionLocal() as db:
        child = db.get(TaskNode, "fast-retry-transport-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})
        events = db.query(EventRecord).filter(EventRecord.task_id == child.id, EventRecord.type == "task.fast_retryable_block_requeued").all()

    assert child.status == TaskStatus.QUEUED.value
    assert child.error_message is None
    assert meta["block_report"]["category"] == "transport"
    assert meta["fast_retryable_block_retries"] == 1
    assert events


def test_local_skill_metadata_block_is_fast_requeued_after_runner_isolation():
    orchestrator = Orchestrator(get_settings())
    reason = (
        "Codex acceptance exited with code 1.\n"
        "ERROR codex_core::session::session: failed to load skill "
        "C:/Users/example/.codex/plugins/cache/openai-curated/heygen/202e9242/skills/heygen-avatar/SKILL.md: "
        "invalid description: exceeds maximum length of 1024 characters"
    )

    with SessionLocal() as db:
        root = TaskNode(id="fast-retry-skill-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="fast-retry-skill-child",
            parent_id=root.id,
            title="Local skill metadata child",
            description="Blocked by a local plugin skill metadata error.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "fast-retry-skill-child",
                        "title": "Local skill metadata child",
                        "status": "blocked",
                        "retryable": False,
                        "category": "tool_internal",
                        "parent_action": "pause_tool_fix",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("fast-retry-skill-root") == 1

    with SessionLocal() as db:
        child = db.get(TaskNode, "fast-retry-skill-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})
        events = db.query(EventRecord).filter(EventRecord.task_id == child.id, EventRecord.type == "task.fast_retryable_block_requeued").all()

    assert child.status == TaskStatus.QUEUED.value
    assert child.error_message is None
    assert meta["block_report"]["category"] == "local_skill_metadata"
    assert meta["block_report"]["retryable"] is True
    assert meta["block_report"]["parent_action"] == "wait_and_retry"
    assert meta["block_report"]["reclassified_from"]["category"] == "tool_internal"
    assert meta["fast_retryable_block_retries"] == 1
    assert events


def test_workspace_write_readonly_sandbox_block_is_fast_requeued():
    orchestrator = Orchestrator(get_settings())
    reason = (
        "无法完成该叶子目标：当前会话文件系统权限为 read-only，且 approval policy=never。"
        "已读取 combat 相关接口并确认应新增 scripts/combat/CombatBidirectionalSmokeTest.gd，"
        "但新增文件的 apply_patch 被沙箱拒绝，无法满足必须修改文件的验收条件。"
    )

    with SessionLocal() as db:
        root = TaskNode(id="fast-retry-sandbox-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="fast-retry-sandbox-child",
            parent_id=root.id,
            title="Workspace sandbox child",
            description="Blocked by an incorrectly readonly workspace-write worker.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "fast-retry-sandbox-child",
                        "title": "Workspace sandbox child",
                        "status": "blocked",
                        "source": "worker.blocked",
                        "retryable": False,
                        "category": "external",
                        "parent_action": "manual_review",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("fast-retry-sandbox-root") == 1

    with SessionLocal() as db:
        child = db.get(TaskNode, "fast-retry-sandbox-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})
        events = db.query(EventRecord).filter(EventRecord.task_id == child.id, EventRecord.type == "task.fast_retryable_block_requeued").all()

    assert child.status == TaskStatus.QUEUED.value
    assert child.error_message is None
    assert meta["block_report"]["category"] == "workspace_sandbox"
    assert meta["block_report"]["retryable"] is True
    assert meta["block_report"]["parent_action"] == "wait_and_retry"
    assert meta["block_report"]["reclassified_from"]["category"] == "external"
    assert meta["fast_retryable_block_retries"] == 1
    assert meta["fast_retryable_block_retries_by_category"]["workspace_sandbox"] == 1
    assert events


def test_planner_readonly_sandbox_block_is_not_workspace_write_fast_retry():
    orchestrator = Orchestrator(get_settings())
    reason = (
        "Cannot create docs/recovery/source_inventory.json because the current filesystem sandbox is read-only. "
        "Filesystem permission profile grants read access only to the workspace root."
    )

    with SessionLocal() as db:
        root = TaskNode(id="planner-sandbox-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="planner-sandbox-child",
            parent_id=root.id,
            title="Planner sandbox child",
            description="Planner is supposed to be readonly.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "planner-sandbox-child",
                        "title": "Planner sandbox child",
                        "status": "blocked",
                        "source": "planner.blocked",
                        "retryable": False,
                        "category": "external",
                        "parent_action": "manual_review",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("planner-sandbox-root") == 0

    with SessionLocal() as db:
        child = db.get(TaskNode, "planner-sandbox-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})

    assert child.status == TaskStatus.BLOCKED.value
    assert meta["block_report"]["category"] == "external"


def test_workspace_write_chinese_readonly_sandbox_block_is_fast_requeued():
    orchestrator = Orchestrator(get_settings())
    reason = "已完成取证，但当前会话文件系统为只读且 approval policy=never，写入补丁被拒绝，无法创建要求的证据文件。"

    with SessionLocal() as db:
        root = TaskNode(id="fast-retry-chinese-sandbox-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="fast-retry-chinese-sandbox-child",
            parent_id=root.id,
            title="Chinese readonly sandbox child",
            description="Blocked by an incorrectly readonly workspace-write worker.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "fast-retry-chinese-sandbox-child",
                        "title": "Chinese readonly sandbox child",
                        "status": "blocked",
                        "source": "worker.blocked",
                        "retryable": False,
                        "category": "external",
                        "parent_action": "skill_lookup_recovery",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("fast-retry-chinese-sandbox-root") == 1

    with SessionLocal() as db:
        child = db.get(TaskNode, "fast-retry-chinese-sandbox-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})

    assert child.status == TaskStatus.QUEUED.value
    assert meta["block_report"]["category"] == "workspace_sandbox"
    assert meta["block_report"]["retryable"] is True
    assert "skill_lookup" not in meta["block_report"]


def test_false_child_tool_capability_block_is_protocol_fast_requeued():
    orchestrator = Orchestrator(get_settings())
    reason = (
        "无法执行验证命令：当前可用工具集没有提供命令执行能力（只有 shell_command 需 commentary，"
        "但本响应格式要求 commentary 为 codex_output_schema）。因此不能独立核验证据文件。"
    )

    with SessionLocal() as db:
        root = TaskNode(id="fast-retry-tool-capability-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="fast-retry-tool-capability-child",
            parent_id=root.id,
            title="Tool capability child",
            description="Blocked by a false tool capability claim.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "fast-retry-tool-capability-child",
                        "title": "Tool capability child",
                        "status": "blocked",
                        "source": "verifier.blocked",
                        "retryable": False,
                        "category": "external",
                        "parent_action": "skill_lookup_recovery",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("fast-retry-tool-capability-root") == 1

    with SessionLocal() as db:
        child = db.get(TaskNode, "fast-retry-tool-capability-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})

    assert child.status == TaskStatus.QUEUED.value
    assert meta["block_report"]["category"] == "protocol"
    assert meta["block_report"]["retryable"] is True
    assert meta["block_report"]["parent_action"] == "wait_and_retry"


def test_local_skill_metadata_retry_limit_is_separate_from_prior_transport_retries():
    orchestrator = Orchestrator(get_settings())
    reason = (
        "Codex acceptance exited with code 1.\n"
        "ERROR codex_core::session::session: failed to load skill "
        "C:/Users/example/.codex/plugins/cache/openai-curated/heygen/202e9242/skills/heygen-video/SKILL.md: "
        "invalid description: exceeds maximum length of 1024 characters\n"
        "rmcp::transport::worker: worker quit with fatal: Transport channel closed"
    )

    with SessionLocal() as db:
        root = TaskNode(id="fast-retry-skill-after-transport-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="fast-retry-skill-after-transport-child",
            parent_id=root.id,
            title="Local skill metadata after transport child",
            description="Blocked by local plugin metadata after previous transport retries.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "fast_retryable_block_retries": 2,
                    "last_fast_retryable_block_retry_reason": "transport",
                    "block_report": {
                        "task_id": "fast-retry-skill-after-transport-child",
                        "title": "Local skill metadata after transport child",
                        "status": "blocked",
                        "retryable": True,
                        "category": "transport",
                        "parent_action": "wait_and_retry",
                        "reason": reason,
                    },
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("fast-retry-skill-after-transport-root") == 1

    with SessionLocal() as db:
        child = db.get(TaskNode, "fast-retry-skill-after-transport-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})

    assert child.status == TaskStatus.QUEUED.value
    assert meta["block_report"]["category"] == "local_skill_metadata"
    assert meta["fast_retryable_block_retries"] == 1
    assert meta["fast_retryable_block_retries_by_category"]["local_skill_metadata"] == 1


def test_missing_final_response_block_is_protocol_fast_requeued():
    orchestrator = Orchestrator(get_settings())
    reason = "Codex 没有写出最终响应文件。"

    with SessionLocal() as db:
        root = TaskNode(id="fast-retry-protocol-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="fast-retry-protocol-child",
            parent_id=root.id,
            title="Protocol child",
            description="Blocked by protocol output.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "fast-retry-protocol-child",
                        "title": "Protocol child",
                        "status": "blocked",
                        "source": "verifier.blocked",
                        "retryable": False,
                        "category": "external",
                        "parent_action": "skill_lookup_recovery",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("fast-retry-protocol-root") == 1

    with SessionLocal() as db:
        child = db.get(TaskNode, "fast-retry-protocol-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})

    assert child.status == TaskStatus.QUEUED.value
    assert child.error_message is None
    assert meta["block_report"]["category"] == "protocol"
    assert meta["block_report"]["retryable"] is True
    assert meta["block_report"]["parent_action"] == "wait_and_retry"
    assert "skill_lookup" not in meta["block_report"]


def test_action_plan_failed_leaf_is_protocol_fast_requeued():
    orchestrator = Orchestrator(get_settings())
    reason = "我将先做只读盘点，再生成父层合并文件并验证完整性；这个叶子任务边界清晰，可以在当前工作树内 300% 完成。"

    with SessionLocal() as db:
        root = TaskNode(id="fast-retry-plan-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="fast-retry-plan-child",
            parent_id=root.id,
            title="Plan child",
            description="Model returned an action plan instead of final JSON.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message=reason,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "fast-retry-plan-child",
                        "title": "Plan child",
                        "status": "blocked",
                        "source": "worker.failed",
                        "retryable": False,
                        "category": "external",
                        "parent_action": "skill_lookup_recovery",
                        "reason": reason,
                    }
                }
            ),
        )
        db.add_all([root, blocked])
        db.commit()

    assert orchestrator._auto_retry_fast_retryable_blocks("fast-retry-plan-root") == 1

    with SessionLocal() as db:
        child = db.get(TaskNode, "fast-retry-plan-child")
        assert child is not None
        meta = loads(child.node_meta_json, {})

    assert child.status == TaskStatus.QUEUED.value
    assert meta["block_report"]["category"] == "protocol"
    assert meta["block_report"]["retryable"] is True


@pytest.mark.asyncio
async def test_auto_concurrency_uses_codex_health_retry_after_as_resource_pressure(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reset_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    (workspace / "codex_health.json").write_text(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "resource_block",
                "summary": "codex_doctor_resource_backoff",
                "quota_observability": "retry_after_observed_from_codex_output",
                "quota_retry_after_seconds": 3600,
                "quota_reset_at": reset_at,
                "quota_retry_after_source": "codex_output",
                "quota_source": "doctor",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=workspace,
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        auto_concurrency_queue_threshold=6,
    )
    orchestrator = Orchestrator(settings)
    await orchestrator._agent_semaphore.set_capacity(13)

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-health-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        children = [
            TaskNode(id=f"auto-concurrency-health-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(30)
        ]
        db.add_all([root, *children])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-health-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-health-root")
        state = orchestrator.get_watchdog_state(db, root_id="auto-concurrency-health-root")

    assert orchestrator._agent_semaphore.capacity == 1
    assert tree["stats"]["auto_concurrency_mode"] == "resource_cooldown"
    assert tree["stats"]["auto_concurrency_resource_pressure"] == 1
    assert 0 < state["codex_quota_retry_after_seconds"] <= 3600
    assert state["codex_quota_reset_at"].isoformat() == reset_at


@pytest.mark.asyncio
async def test_auto_concurrency_ignores_elapsed_codex_health_retry_after(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reset_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    (workspace / "codex_health.json").write_text(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "resource_block",
                "summary": "codex_runtime_retry_after_observed",
                "quota_observability": "retry_after_observed_from_codex_output",
                "quota_retry_after_seconds": 3600,
                "quota_reset_at": reset_at,
                "quota_retry_after_source": "codex_output",
                "quota_source": "run_log:runs/node-a/worker.jsonl",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=workspace,
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        auto_concurrency_queue_threshold=6,
    )
    orchestrator = Orchestrator(settings)
    await orchestrator._agent_semaphore.set_capacity(1)

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-expired-health-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        children = [
            TaskNode(id=f"auto-concurrency-expired-health-child-{index}", parent_id=root.id, title=f"Child {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(30)
        ]
        db.add_all([root, *children])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-expired-health-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-expired-health-root")
        state = orchestrator.get_watchdog_state(db, root_id="auto-concurrency-expired-health-root")

    assert orchestrator._agent_semaphore.capacity == 13
    assert tree["stats"]["auto_concurrency_mode"] == "high_throughput"
    assert tree["stats"]["auto_concurrency_resource_pressure"] == 0
    assert state["codex_health_status"] == "warning"
    assert state["codex_quota_retry_after_seconds"] == 0


@pytest.mark.asyncio
async def test_auto_concurrency_scales_down_on_active_resource_log_pressure(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=3,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=3,
        auto_concurrency_queue_threshold=3,
    )
    orchestrator = Orchestrator(settings)
    active_child_id = "auto-concurrency-active-rate-limit-child"
    run_dir = settings.workspace_dir / "runs" / active_child_id
    run_dir.mkdir(parents=True)
    (run_dir / "worker.jsonl").write_text('{"message":"429 rate limit, retry later"}\n', encoding="utf-8")

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-active-pressure-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        active = TaskNode(
            id=active_child_id,
            parent_id=root.id,
            title="Active child with rate pressure",
            description="Worker saw a resource signal.",
            depth=1,
            status=TaskStatus.WORKING.value,
        )
        queued = [
            TaskNode(id=f"auto-concurrency-active-pressure-queued-{index}", parent_id=root.id, title=f"Queued {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(4)
        ]
        db.add_all([root, active, *queued])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-active-pressure-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-active-pressure-root")
        state = orchestrator.get_watchdog_state(db, root_id="auto-concurrency-active-pressure-root")

    assert orchestrator._agent_semaphore.capacity == 1
    assert tree["stats"]["auto_concurrency_mode"] == "resource_cooldown"
    assert tree["stats"]["auto_concurrency_resource_pressure"] == 1
    assert state["active_resource_pressure"] is True
    assert state["budget_warning_level"] == "resource_pressure"
    assert "下一次调度会进入低功率冷却" in state["budget_warning_message"]


@pytest.mark.asyncio
async def test_auto_concurrency_ignores_agent_message_usage_limit_evidence(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        max_concurrency=3,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=3,
        auto_concurrency_queue_threshold=3,
    )
    orchestrator = Orchestrator(settings)
    await orchestrator._agent_semaphore.set_capacity(3)
    active_child_id = "auto-concurrency-agent-history-child"
    run_dir = settings.workspace_dir / "runs" / active_child_id
    run_dir.mkdir(parents=True)
    run_dir.joinpath("worker.jsonl").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(
                        {
                            "summary": "Historical report quotes: You've hit your usage limit.",
                            "evidence": ["turn.failed: usage limit was already diagnosed."],
                        }
                    ),
                },
            }
        ),
        encoding="utf-8",
    )

    with SessionLocal() as db:
        root = TaskNode(id="auto-concurrency-agent-history-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        active = TaskNode(
            id=active_child_id,
            parent_id=root.id,
            title="Active child with historical usage report",
            description="Worker quotes a past resource signal.",
            depth=1,
            status=TaskStatus.WORKING.value,
        )
        queued = [
            TaskNode(id=f"auto-concurrency-agent-history-queued-{index}", parent_id=root.id, title=f"Queued {index}", description="", depth=1, status=TaskStatus.QUEUED.value)
            for index in range(6)
        ]
        db.add_all([root, active, *queued])
        db.commit()

    await orchestrator._adjust_agent_concurrency("auto-concurrency-agent-history-root", orchestrator._agent_semaphore)

    with SessionLocal() as db:
        tree = orchestrator.get_tree(db, root_id="auto-concurrency-agent-history-root")

    assert orchestrator._agent_semaphore.capacity == 3
    assert tree["stats"]["auto_concurrency_mode"] != "resource_cooldown"
    assert tree["stats"]["auto_concurrency_resource_pressure"] == 0


@pytest.mark.asyncio
async def test_pending_worker_result_resumes_at_verifier_without_rerunning_worker():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    runner = PendingVerifierRunner()
    worker_result = WorkerResult(
        status="succeeded",
        summary="Worker already finished.",
        changed_files=["project.godot"],
        evidence=["worker evidence"],
    )

    with SessionLocal() as db:
        task = TaskNode(
            id="pending-verifier",
            parent_id=None,
            title="Pending verifier",
            description="Verify existing worker output.",
            depth=0,
            status=TaskStatus.QUEUED.value,
            result_json=dumps({"worker": worker_result.model_dump()}),
        )
        db.add(task)
        db.commit()

    await orchestrator._process_node("pending-verifier", runner, asyncio.Semaphore(1))

    with SessionLocal() as db:
        task = db.get(TaskNode, "pending-verifier")
        assert task is not None
        runs = db.query(RunRecord).filter(RunRecord.task_id == "pending-verifier").all()
        result = loads(task.result_json, {})

    assert runner.verify_calls == 1
    assert task.status == TaskStatus.DONE.value
    assert result["worker"]["summary"] == "Worker already finished."
    assert result["verifier"]["reason"] == "Recovered verifier passed."
    assert [run.kind for run in runs] == [RunKind.VERIFIER.value]


@pytest.mark.asyncio
async def test_pending_worker_result_gets_ready_batch_priority():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    runner = SequentialLeafRunner()
    worker_result = WorkerResult(
        status="succeeded",
        summary="Worker already finished.",
        changed_files=["project.godot"],
        evidence=["worker evidence"],
    )

    with SessionLocal() as db:
        root = TaskNode(id="root", parent_id=None, title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        normal = TaskNode(id="normal-child", parent_id="root", title="Normal", description="Needs full run.", depth=1)
        pending = TaskNode(
            id="pending-child",
            parent_id="root",
            title="Pending",
            description="Needs verifier only.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            result_json=dumps({"worker": worker_result.model_dump()}),
        )
        db.add_all([root, normal, pending])
        db.commit()

    await orchestrator._process_children_with_dependencies("root", ["normal-child", "pending-child"], runner, asyncio.Semaphore(1))

    assert runner.events[0] == "verify:pending-child"

    with SessionLocal() as db:
        children = db.query(TaskNode).filter(TaskNode.parent_id == "root").all()

    assert {child.status for child in children} == {TaskStatus.DONE.value}


@pytest.mark.asyncio
async def test_dependency_scheduler_defers_unfinished_ready_child_without_busy_loop():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        max_concurrency=2,
        scheduler_deferred_retry_seconds=-1,
    )
    orchestrator = Orchestrator(settings)
    processed: list[str] = []

    with SessionLocal() as db:
        root = TaskNode(id="defer-root", parent_id=None, title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        waiting = TaskNode(id="waiting-child", parent_id=root.id, title="Waiting", description="Still has nested work.", depth=1, status=TaskStatus.QUEUED.value)
        base = TaskNode(id="base-child", parent_id=root.id, title="Base", description="Unblocks dependent.", depth=1, status=TaskStatus.QUEUED.value)
        dependent = TaskNode(
            id="dependent-child",
            parent_id=root.id,
            title="Dependent",
            description="Depends on base.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps({"depends_on_ids": [base.id]}),
        )
        db.add_all([root, waiting, base, dependent])
        db.commit()

    async def fake_process_node(task_id: str, runner: BaseCodexRunner, semaphore: asyncio.Semaphore) -> None:
        processed.append(task_id)
        with SessionLocal() as db:
            task = db.get(TaskNode, task_id)
            assert task is not None
            if task_id == "waiting-child":
                task.status = TaskStatus.WAITING_CHILDREN.value
            else:
                task.status = TaskStatus.DONE.value
            db.commit()

    orchestrator._process_node = fake_process_node  # type: ignore[method-assign]
    await orchestrator._process_children_with_dependencies(
        "defer-root",
        ["waiting-child", "base-child", "dependent-child"],
        SlowPlanningRunner(),
        asyncio.Semaphore(2),
    )

    with SessionLocal() as db:
        waiting = db.get(TaskNode, "waiting-child")
        base = db.get(TaskNode, "base-child")
        dependent = db.get(TaskNode, "dependent-child")
        events = db.query(EventRecord).filter(EventRecord.task_id == "defer-root", EventRecord.type == "scheduler.child_deferred").all()

    assert waiting is not None and waiting.status == TaskStatus.WAITING_CHILDREN.value
    assert base is not None and base.status == TaskStatus.DONE.value
    assert dependent is not None and dependent.status == TaskStatus.DONE.value
    assert processed.count("waiting-child") == 1
    assert "dependent-child" in processed
    assert events
    assert "waiting-child" in loads(events[-1].payload_json, {})["child_ids"]


@pytest.mark.asyncio
async def test_dependency_scheduler_schedules_deferred_parent_resume():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        max_concurrency=1,
        scheduler_deferred_retry_seconds=0,
    )
    orchestrator = Orchestrator(settings)
    processed: list[str] = []
    resumed = asyncio.Event()

    with SessionLocal() as db:
        root = TaskNode(id="deferred-root", parent_id=None, title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        waiting = TaskNode(id="waiting-child", parent_id=root.id, title="Waiting", description="Still has nested work.", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, waiting])
        db.commit()

    async def fake_process_node(task_id: str, runner: BaseCodexRunner, semaphore: asyncio.Semaphore) -> None:
        processed.append(task_id)
        with SessionLocal() as db:
            task = db.get(TaskNode, task_id)
            assert task is not None
            if task_id == "waiting-child":
                task.status = TaskStatus.WAITING_CHILDREN.value
            db.commit()
        if task_id == "deferred-root":
            resumed.set()

    orchestrator._process_node = fake_process_node  # type: ignore[method-assign]
    await orchestrator._process_children_with_dependencies(
        "deferred-root",
        ["waiting-child"],
        SlowPlanningRunner(),
        asyncio.Semaphore(1),
    )
    await asyncio.wait_for(resumed.wait(), timeout=30)

    with SessionLocal() as db:
        events = db.query(EventRecord).filter(EventRecord.task_id == "deferred-root").all()

    assert processed[:2] == ["waiting-child", "deferred-root"]
    assert any(event.type == "scheduler.child_deferred" for event in events)
    assert any(event.type == "scheduler.deferred_resume" for event in events)


def test_recover_deferred_parent_resumes_finds_waiting_parents_after_restart():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        max_concurrency=5,
        scheduler_deferred_retry_seconds=60,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="recovered-root", parent_id=None, title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        parent = TaskNode(
            id="recovered-parent",
            parent_id=root.id,
            title="Recovered parent",
            description="Was waiting when API restarted.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        queued = TaskNode(
            id="recovered-queued-child",
            parent_id=parent.id,
            title="Queued child",
            description="Needs resumed processing.",
            depth=2,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, parent, queued])
        db.commit()

        candidates = orchestrator._deferred_parent_resume_candidates(db, root.id)

    assert candidates == [("recovered-parent", ["recovered-queued-child"])]


def test_recover_deferred_parent_resumes_finds_queued_parent_ready_for_acceptance():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        max_concurrency=5,
        scheduler_deferred_retry_seconds=60,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="acceptance-recovered-root", parent_id=None, title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        parent = TaskNode(
            id="acceptance-recovered-parent",
            parent_id=root.id,
            title="Recovered parent acceptance",
            description="All children were done when API restarted.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        done_child = TaskNode(
            id="acceptance-recovered-done-child",
            parent_id=parent.id,
            title="Done child",
            description="Already finished.",
            depth=2,
            status=TaskStatus.DONE.value,
        )
        db.add_all([root, parent, done_child])
        db.commit()

        candidates = orchestrator._deferred_parent_resume_candidates(db, root.id)

    assert candidates == [("acceptance-recovered-parent", [])]


def test_settles_unreachable_descendants_under_terminal_parent_before_acceptance_recovery():
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        max_concurrency=5,
        scheduler_deferred_retry_seconds=60,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="terminal-stale-root", parent_id=None, title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        parent = TaskNode(
            id="terminal-stale-parent",
            parent_id=root.id,
            title="Queued parent acceptance",
            description="All direct children are done.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        done_child = TaskNode(
            id="terminal-stale-done-child",
            parent_id=parent.id,
            title="Done child",
            description="Already accepted.",
            depth=2,
            status=TaskStatus.DONE.value,
        )
        stale_grandchild = TaskNode(
            id="terminal-stale-grandchild",
            parent_id=done_child.id,
            title="Stale grandchild",
            description="Cannot run below a done parent.",
            depth=3,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, parent, done_child, stale_grandchild])
        db.commit()

        settled = orchestrator._settle_unreachable_descendants_under_terminal_ancestors(db, root.id)
        db.commit()
        candidates = orchestrator._deferred_parent_resume_candidates(db, root.id)
        stale = db.get(TaskNode, stale_grandchild.id)

    assert settled == {"terminal-stale-grandchild"}
    assert candidates == [("terminal-stale-parent", [])]
    assert stale is not None
    assert stale.status == TaskStatus.CANCELED.value
    result = loads(stale.result_json, {})
    assert result["system"]["status"] == "canceled_unreachable_due_to_terminal_ancestor"
    assert result["system"]["ancestor_id"] == "terminal-stale-done-child"


@pytest.mark.asyncio
async def test_reserve_agent_start_waits_when_external_codex_slots_are_full(monkeypatch):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        max_concurrency=2,
        auto_concurrency_enabled=False,
        runner_mode="real",
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": 1, "task_id": "old-a"}, {"pid": 2, "task_id": "old-b"}],
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="external-slot-root",
            parent_id=None,
            title="Root",
            description="Still running elsewhere.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"runner_mode": "real"}),
        )
        task = TaskNode(id="external-slot-task", parent_id=root.id, title="Task", description="Needs a slot.", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, task])
        db.commit()

    allowed = await orchestrator._reserve_agent_start("external-slot-task", "worker")

    assert allowed is False
    with SessionLocal() as db:
        stored = db.get(TaskNode, "external-slot-task")
        assert stored is not None
        assert stored.status == TaskStatus.QUEUED.value
        event = db.query(EventRecord).filter(EventRecord.task_id == "external-slot-task", EventRecord.type == "scheduler.external_codex_slots_full").one()
        payload = loads(event.payload_json, {})
        assert payload["external_codex_processes"] == 2
        assert payload["capacity"] == 2


@pytest.mark.asyncio
async def test_reserve_agent_start_counts_pending_slot_before_process_is_visible(monkeypatch, tmp_path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        max_concurrency=13,
        auto_concurrency_enabled=False,
        runner_mode="real",
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": index, "task_id": f"old-{index}"} for index in range(12)],
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="pending-slot-root",
            parent_id=None,
            title="Root",
            description="Reserve the last slot once.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"runner_mode": "real"}),
        )
        first = TaskNode(id="pending-slot-first", parent_id=root.id, title="First", description="Needs a slot.", depth=1, status=TaskStatus.QUEUED.value)
        second = TaskNode(id="pending-slot-second", parent_id=root.id, title="Second", description="Must wait.", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, first, second])
        db.commit()

    assert await orchestrator._reserve_agent_start("pending-slot-first", "worker") is True
    assert await orchestrator._reserve_agent_start("pending-slot-second", "worker") is False

    with SessionLocal() as db:
        event = db.query(EventRecord).filter(EventRecord.task_id == "pending-slot-second", EventRecord.type == "scheduler.external_codex_slots_full").one()
        payload = loads(event.payload_json, {})

    assert payload["external_codex_processes"] == 12
    assert payload["effective_codex_slots"] == 13
    assert payload["pending_codex_slot_reservations"] == ["pending-slot-first"]


@pytest.mark.asyncio
async def test_reserve_agent_start_counts_shared_pending_slot_across_orchestrators(monkeypatch, tmp_path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        max_concurrency=13,
        auto_concurrency_enabled=False,
        runner_mode="real",
    )
    first_orchestrator = Orchestrator(settings)
    second_orchestrator = Orchestrator(settings)

    def fake_external(max_cache_age_seconds=2.0):
        return [{"pid": index, "task_id": f"old-{index}"} for index in range(12)]

    monkeypatch.setattr(first_orchestrator, "_external_codex_processes", fake_external)
    monkeypatch.setattr(second_orchestrator, "_external_codex_processes", fake_external)

    with SessionLocal() as db:
        root = TaskNode(
            id="shared-pending-root",
            parent_id=None,
            title="Root",
            description="Share pending slot reservations.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"runner_mode": "real"}),
        )
        first = TaskNode(
            id="shared-pending-first",
            parent_id=root.id,
            title="First",
            description="Reserves a cross-process slot.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        second = TaskNode(
            id="shared-pending-second",
            parent_id=root.id,
            title="Second",
            description="Sees the reservation from another orchestrator.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, first, second])
        db.commit()

    assert await first_orchestrator._reserve_agent_start("shared-pending-first", "worker") is True
    assert await second_orchestrator._reserve_agent_start("shared-pending-second", "worker") is False

    with SessionLocal() as db:
        event = (
            db.query(EventRecord)
            .filter(EventRecord.task_id == "shared-pending-second", EventRecord.type == "scheduler.external_codex_slots_full")
            .one()
        )
        payload = loads(event.payload_json, {})

    assert payload["external_codex_processes"] == 12
    assert payload["effective_codex_slots"] == 13
    assert payload["pending_codex_slot_reservations"] == ["shared-pending-first"]
    assert (tmp_path / "codex_slot_reservations.json").is_file()


@pytest.mark.asyncio
async def test_reserve_agent_start_waits_when_same_task_has_external_codex_process(monkeypatch):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        max_concurrency=5,
        auto_concurrency_enabled=False,
        runner_mode="real",
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": 1, "task_id": "same-task"}],
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="same-task-root",
            parent_id=None,
            title="Root",
            description="Still running elsewhere.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"runner_mode": "real"}),
        )
        task = TaskNode(id="same-task", parent_id=root.id, title="Task", description="Already has an orphan process.", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, task])
        db.commit()

    allowed = await orchestrator._reserve_agent_start("same-task", "worker")

    assert allowed is False
    with SessionLocal() as db:
        event = db.query(EventRecord).filter(EventRecord.task_id == "same-task", EventRecord.type == "scheduler.external_codex_slots_full").one()
        payload = loads(event.payload_json, {})
        assert payload["external_codex_processes"] == 1
        assert payload["capacity"] == 5
        assert payload["same_task_is_running"] is True


@pytest.mark.asyncio
async def test_reserve_agent_start_ignores_external_codex_slots_for_mock_runner(monkeypatch):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        max_concurrency=1,
        runner_mode="mock",
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": 1, "task_id": "real-tree-a"}, {"pid": 2, "task_id": "real-tree-b"}],
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="mock-external-slot-root",
            parent_id=None,
            title="Mock Root",
            description="Mock work should not wait for real Codex slots.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"runner_mode": "mock"}),
        )
        task = TaskNode(id="mock-external-slot-task", parent_id=root.id, title="Task", description="Needs no real slot.", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, task])
        db.commit()

    allowed = await orchestrator._reserve_agent_start("mock-external-slot-task", "worker")

    assert allowed is True
    with SessionLocal() as db:
        events = db.query(EventRecord).filter(EventRecord.task_id == "mock-external-slot-task", EventRecord.type == "scheduler.external_codex_slots_full").all()
    assert events == []


def test_watchdog_observes_external_codex_slots_instead_of_resuming_orphan(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        max_concurrency=2,
        watchdog_orphan_resume_after_seconds=0,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": 1, "task_id": "old-a"}, {"pid": 2, "task_id": "old-b"}],
    )

    with SessionLocal() as db:
        root = TaskNode(id="external-watchdog-root", parent_id=None, title="Root", description="Has work.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        child = TaskNode(id="external-watchdog-child", parent_id=root.id, title="Queued child", description="Queued.", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, child])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root.id)

    assert state["active_agents"] == 2
    assert state["active_agent_statuses"] == 0
    assert state["codex_slots_active"] == 2
    assert state["orphaned_active_tree"] is False
    assert state["should_resume"] is False
    assert state["recommended_action"] == "observe_running"


def test_external_codex_slot_filter_ignores_terminal_task_processes():
    orchestrator = Orchestrator(get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir))
    with SessionLocal() as db:
        done_task = TaskNode(id="done-process-task", title="Done", description="", depth=0, status=TaskStatus.DONE.value)
        canceled_task = TaskNode(id="canceled-process-task", title="Canceled", description="", depth=0, status=TaskStatus.CANCELED.value)
        queued_task = TaskNode(id="queued-process-task", title="Queued", description="", depth=0, status=TaskStatus.QUEUED.value)
        db.add_all([done_task, canceled_task, queued_task])
        db.commit()

    filtered = orchestrator._filter_nonterminal_external_codex_processes(
        [
            {"pid": 1, "task_id": "done-process-task"},
            {"pid": 2, "task_id": "canceled-process-task"},
            {"pid": 3, "task_id": "queued-process-task"},
            {"pid": 4, "task_id": "unknown-process-task"},
            {"pid": 5, "task_id": ""},
        ]
    )

    assert filtered == [
        {"pid": 3, "task_id": "queued-process-task"},
        {"pid": 4, "task_id": "unknown-process-task"},
        {"pid": 5, "task_id": ""},
    ]


def test_l4_guardian_state_is_embedded_in_watchdog(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        max_concurrency=2,
        machine_safety_min_available_memory_mb=4096,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": 11, "task_id": "guardian-child"}],
    )
    monkeypatch.setattr(
        orchestrator,
        "_machine_safety_pressure",
        lambda: (
            True,
            {
                "enabled": True,
                "available_memory_mb": 512,
                "safe_min_available_memory_mb": 4096,
            },
        ),
    )

    with SessionLocal() as db:
        root = TaskNode(
            id="guardian-root",
            parent_id=None,
            title="Root",
            description="Watch the system.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        child = TaskNode(
            id="guardian-child",
            parent_id=root.id,
            title="Child",
            description="Running.",
            depth=1,
            status=TaskStatus.WORKING.value,
        )
        db.add_all([root, child])
        db.commit()

        state = orchestrator.get_watchdog_state(db, root.id)

    assert state["guardian_level"] == "L4"
    assert state["guardian_status"] == "needs_attention"
    assert state["guardian_risk_level"] == "critical"
    assert state["guardian_state_path"].endswith("l4_guardian_state.json")
    check_keys = {check["key"] for check in state["guardian_checks"]}
    assert {"mission", "machine", "process_slots", "agent_progress"}.issubset(check_keys)


@pytest.mark.asyncio
async def test_parent_converts_nonretryable_blocked_child_to_repair_task():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(id="repair-parent", title="Parent", description="Integrate children.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="blocked-child",
            parent_id=parent.id,
            title="Blocked child",
            description="Original child failed.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message="工作目录为空，无法完成原子任务。",
        )
        db.add_all([parent, blocked])
        db.commit()

    await orchestrator._accept_parent("repair-parent", SlowPlanningRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        parent = db.get(TaskNode, "repair-parent")
        blocked = db.get(TaskNode, "blocked-child")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "repair-parent").all()
        repair_children = [child for child in children if child.id != "blocked-child"]
        events = db.query(EventRecord).filter(EventRecord.task_id == "repair-parent").all()

    assert parent is not None and parent.status == TaskStatus.DONE.value
    assert blocked is not None and blocked.status == TaskStatus.BLOCKED.value
    blocker_meta = loads(blocked.node_meta_json, {})
    assert blocker_meta["superseded_by_repair_child_ids"]
    assert len(repair_children) == 1
    assert repair_children[0].status == TaskStatus.DONE.value
    assert repair_children[0].id in blocker_meta["superseded_by_repair_child_ids"]
    assert any(event.type == "parent.blocked_child_repair_scheduled" for event in events)


@pytest.mark.asyncio
async def test_parent_preemptively_repairs_failed_child_before_skill_note_queue():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(id="preempt-parent", title="Parent", description="Integrate children.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        failed = TaskNode(
            id="failed-child",
            parent_id=parent.id,
            title="Failed child",
            description="Original child failed.",
            depth=1,
            status=TaskStatus.FAILED.value,
            error_message="Global resource integrity failed.",
        )
        skill_note = TaskNode(
            id="skill-note-child",
            parent_id=parent.id,
            title="Parent skill note",
            description="Consolidate lessons after children finish.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps(
                {
                    "coverage_ids": ["codex-battlefield-skill-note"],
                    "verification_kind": "skill-note consolidation",
                    "depends_on_ids": ["failed-child"],
                }
            ),
        )
        db.add_all([parent, failed, skill_note])
        db.commit()

    await orchestrator._process_node("preempt-parent", SlowPlanningRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        parent = db.get(TaskNode, "preempt-parent")
        failed = db.get(TaskNode, "failed-child")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "preempt-parent").all()
        repair_children = [child for child in children if child.id not in {"failed-child", "skill-note-child"}]
        events = db.query(EventRecord).filter(EventRecord.task_id == "preempt-parent").all()

    assert parent is not None and parent.status == TaskStatus.DONE.value
    assert failed is not None and failed.status == TaskStatus.FAILED.value
    failed_meta = loads(failed.node_meta_json, {})
    assert failed_meta["superseded_by_repair_child_ids"]
    assert len(repair_children) == 1
    assert repair_children[0].status == TaskStatus.DONE.value
    assert repair_children[0].id in failed_meta["superseded_by_repair_child_ids"]
    assert any(event.type == "parent.blocked_child_repair_preempted" for event in events)


@pytest.mark.asyncio
async def test_verifier_visual_uncertain_blocks_for_human_visual_review():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    constitution_id = await locked_constitution(orchestrator, "Test visual uncertainty.")
    root_id = await orchestrator.start_project("Test visual uncertainty.", constitution_id=constitution_id, auto_run=False)

    await orchestrator._process_node(root_id, VisualUncertainVerifierRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        events = db.query(EventRecord).filter(EventRecord.task_id == root_id).all()

    assert root is not None and root.status == TaskStatus.BLOCKED.value
    meta = loads(root.node_meta_json, {})
    assert meta["block_report"]["category"] == "visual_uncertain"
    assert meta["block_report"]["parent_action"] == "human_visual_review"
    assert meta["block_report"]["human_action_required"] is True
    assert any(event.type == "task.blocked" for event in events)


@pytest.mark.asyncio
async def test_verifier_visual_uncertain_with_children_recurses_before_human_review():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)
    constitution_id = await locked_constitution(orchestrator, "Test visual evidence recursion.")
    root_id = await orchestrator.start_project("Test visual evidence recursion.", constitution_id=constitution_id, auto_run=False)

    await orchestrator._process_node(root_id, VisualUncertainWithEvidenceChildrenRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        children = db.query(TaskNode).filter(TaskNode.parent_id == root_id).all()
        events = db.query(EventRecord).filter(EventRecord.task_id == root_id).all()

    assert root is not None and root.status == TaskStatus.DONE.value
    assert len(children) == 1
    assert children[0].title == "补齐攻击命中视觉帧证据"
    assert children[0].status == TaskStatus.DONE.value
    assert any(event.type == "verifier.visual_uncertain_recursive_evidence" for event in events)
    meta = loads(root.node_meta_json, {})
    assert "block_report" not in meta


@pytest.mark.asyncio
async def test_acceptance_visual_uncertain_with_children_recurses_before_human_review():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(id="visual-parent", title="Parent", description="Integrate visual evidence.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        done = TaskNode(
            id="visual-done-child",
            parent_id=parent.id,
            title="Done child",
            description="Already done.",
            depth=1,
            status=TaskStatus.DONE.value,
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["done.txt"]}, "verifier": {"verdict": "pass"}}),
        )
        db.add_all([parent, done])
        db.commit()

    runner = AcceptanceVisualUncertainWithEvidenceChildrenRunner()
    await orchestrator._accept_parent("visual-parent", runner, orchestrator._agent_semaphore)

    with SessionLocal() as db:
        parent = db.get(TaskNode, "visual-parent")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "visual-parent").all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "visual-parent").all()

    assert parent is not None and parent.status == TaskStatus.DONE.value
    assert runner.accept_calls == 2
    assert len(children) == 2
    repair_children = [child for child in children if child.id != "visual-done-child"]
    assert len(repair_children) == 1
    assert repair_children[0].title == "补齐整合战斗画面证据"
    assert repair_children[0].status == TaskStatus.DONE.value
    assert any(event.type == "acceptance.visual_uncertain_recursive_evidence" for event in events)


@pytest.mark.asyncio
async def test_parent_collects_human_auth_block_without_fake_repair_task():
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, max_concurrency=1)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(id="human-auth-parent", title="Parent", description="Integrate children.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="human-auth-child",
            parent_id=parent.id,
            title="Human auth child",
            description="Needs Workhub skill lookup.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message="Workhub Skill/KM 需要登录 SSO，返回 403 权限不足。",
        )
        db.add_all([parent, blocked])
        db.commit()

    await orchestrator._accept_parent("human-auth-parent", SlowPlanningRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        parent = db.get(TaskNode, "human-auth-parent")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "human-auth-parent").all()
        events = db.query(EventRecord).filter(EventRecord.task_id == "human-auth-parent").all()

    assert parent is not None and parent.status == TaskStatus.WAITING_CHILDREN.value
    assert len(children) == 1
    parent_meta = loads(parent.node_meta_json, {})
    assert parent_meta["blocked_child_reports"][0]["category"] == "human_auth"
    assert parent_meta["blocked_child_reports"][0]["parent_action"] == "human_auth_required"
    assert any(event.type == "parent.human_action_required" for event in events)
    assert not any(event.type == "parent.blocked_child_repair_scheduled" for event in events)


@pytest.mark.asyncio
async def test_parent_waits_for_existing_blocked_child_repair_task_without_duplicate(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        max_concurrency=1,
        full_checkpoint_enabled=False,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(id="pending-repair-parent", title="Parent", description="Integrate children.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="blocked-child",
            parent_id=parent.id,
            title="Blocked child",
            description="Original child failed.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            error_message="不可自动重试的实现失败。",
            node_meta_json=dumps({"superseded_by_repair_child_ids": ["repair-child"]}),
        )
        repair = TaskNode(
            id="repair-child",
            parent_id=parent.id,
            title="修复或替代阻塞子成果：Blocked child",
            description="Repair in progress.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([parent, blocked, repair])
        db.commit()

    await orchestrator._accept_parent("pending-repair-parent", SlowPlanningRunner(), orchestrator._agent_semaphore)

    with SessionLocal() as db:
        parent = db.get(TaskNode, "pending-repair-parent")
        children = db.query(TaskNode).filter(TaskNode.parent_id == "pending-repair-parent").all()
        tree = orchestrator.get_tree(db, root_id="pending-repair-parent", compact_done=False)
        state = orchestrator.get_watchdog_state(db, root_id="pending-repair-parent")

    assert parent is not None and parent.status == TaskStatus.WAITING_CHILDREN.value
    assert [child.id for child in children].count("repair-child") == 1
    assert len(children) == 2
    assert tree["stats"]["blocked"] == 1
    assert tree["stats"]["blocked_active"] == 0
    assert tree["stats"]["blocked_superseded"] == 1
    assert tree["stats"]["blocked_under_repair"] == 1
    assert state["blocked_active"] == 0
    assert state["blocked_superseded"] == 1
    blocker_checks = [check for check in state["guardian_checks"] if check["key"] == "blockers"]
    assert blocker_checks and blocker_checks[0]["status"] == "ok"


def test_blocker_stats_treat_canceled_branch_blocker_as_inactive(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        max_concurrency=1,
        full_checkpoint_enabled=False,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="inactive-branch-root", title="Root", description="Keep going.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        canceled = TaskNode(
            id="inactive-branch-canceled",
            parent_id=root.id,
            title="Canceled branch",
            description="Old branch.",
            depth=1,
            status=TaskStatus.CANCELED.value,
        )
        blocked = TaskNode(
            id="inactive-branch-blocked",
            parent_id=canceled.id,
            title="Blocked stale leaf",
            description="No longer on the active path.",
            depth=2,
            status=TaskStatus.BLOCKED.value,
            error_message="stale blocker under canceled branch",
        )
        db.add_all([root, canceled, blocked])
        db.commit()

        tree = orchestrator.get_tree(db, root_id=root.id, compact_done=False)
        state = orchestrator.get_watchdog_state(db, root_id=root.id)

    assert tree["stats"]["blocked"] == 1
    assert tree["stats"]["blocked_active"] == 0
    assert tree["stats"]["blocked_superseded"] == 1
    assert tree["stats"]["blocked_inactive_branch"] == 1
    assert state["blocked_active"] == 0
    assert state["blocked_inactive_branch"] == 1
    blocker_checks = [check for check in state["guardian_checks"] if check["key"] == "blockers"]
    assert blocker_checks and blocker_checks[0]["status"] == "ok"


def test_blocker_stats_treat_retryable_resource_blocker_as_waiting(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        max_concurrency=1,
        full_checkpoint_enabled=False,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="resource-wait-root", title="Root", description="Keep state.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        blocked = TaskNode(
            id="resource-wait-blocked",
            parent_id=root.id,
            title="Rate limited child",
            description="Retry later.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            node_meta_json=dumps({"block_report": {"category": "rate_limit", "retryable": True, "parent_action": "wait_and_retry"}}),
        )
        transport_quota_blocked = TaskNode(
            id="resource-wait-transport-quota",
            parent_id=root.id,
            title="Transport wrapped quota child",
            description="Retry later.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "category": "rate_limit",
                        "retryable": True,
                        "parent_action": "wait_and_retry",
                        "source": "worker.blocked",
                        "reason": (
                            "Transport channel closed, when UnexpectedContentType(missing-content-type). "
                            "You've hit your usage limit; try again at 11:25 AM."
                        ),
                    }
                }
            ),
        )
        db.add_all([root, blocked, transport_quota_blocked])
        db.commit()

        tree = orchestrator.get_tree(db, root_id=root.id, compact_done=False)
        state = orchestrator.get_watchdog_state(db, root_id=root.id)

    assert tree["stats"]["blocked"] == 2
    assert tree["stats"]["blocked_active"] == 0
    assert tree["stats"]["blocked_resource_wait"] == 2
    assert state["blocked_active"] == 0
    assert state["blocked_resource_wait"] == 2
    blocker_checks = [check for check in state["guardian_checks"] if check["key"] == "blockers"]
    assert blocker_checks and blocker_checks[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_dependency_block_report_names_broken_dependency():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test dependency block report.")
    root_id = await orchestrator.start_project("Test dependency block report.", constitution_id=constitution_id, auto_run=False)
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.status = TaskStatus.WAITING_CHILDREN.value
        dependency = TaskNode(
            id="blocked-dependency",
            parent_id=root.id,
            title="Blocked dependency",
            description="This dependency failed first.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "blocked-dependency",
                        "title": "Blocked dependency",
                        "status": TaskStatus.BLOCKED.value,
                        "source": "worker.blocked",
                        "category": "external",
                        "retryable": False,
                        "parent_action": "manual_review",
                        "reason": "The dependency worktree is empty.",
                    }
                }
            ),
            error_message="The dependency worktree is empty.",
            constitution_id=root.constitution_id,
            constitution_hash=root.constitution_hash,
            constitution_json=root.constitution_json,
        )
        dependent = TaskNode(
            id="dependent-child",
            parent_id=root.id,
            title="Dependent child",
            description="This child needs the dependency first.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps({"depends_on_ids": ["blocked-dependency"]}),
            constitution_id=root.constitution_id,
            constitution_hash=root.constitution_hash,
            constitution_json=root.constitution_json,
        )
        db.add_all([dependency, dependent])
        db.commit()

    await orchestrator._process_children_with_dependencies(root_id, ["dependent-child"], SlowPlanningRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        dependent = db.get(TaskNode, "dependent-child")
        assert dependent is not None
        meta = loads(dependent.node_meta_json, {})

    report = meta["block_report"]
    assert dependent.status == TaskStatus.BLOCKED.value
    assert report["source"] == "dependency.blocked"
    assert report["dependencies"] == ["blocked-dependency"]
    assert report["dependency_reports"][0]["title"] == "Blocked dependency"
    assert report["dependency_reports"][0]["reason"] == "The dependency worktree is empty."
    assert "Blocked dependency" in report["reason"]
    assert "The dependency worktree is empty." in report["reason"]


@pytest.mark.asyncio
async def test_canceled_skill_note_dependency_is_skipped_for_dependent_child(tmp_path: Path):
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace", max_concurrency=1)
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="optional-dependency-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        done_dependency = TaskNode(
            id="done-dependency",
            parent_id=root.id,
            title="本地阻塞与范围缺失根因审计",
            description="Done first.",
            depth=1,
            status=TaskStatus.DONE.value,
        )
        optional_dependency = TaskNode(
            id="optional-skill-notes",
            parent_id=root.id,
            title="skill_notes 与工具流程误判机制审计",
            description="Optional Skill/KM evidence branch.",
            depth=1,
            status=TaskStatus.CANCELED.value,
        )
        dependent = TaskNode(
            id="dependent-with-optional-skill-note",
            parent_id=root.id,
            title="阻塞根因报告整合与验证",
            description="Can continue without optional Skill/KM notes.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps(
                {
                    "depends_on_ids": [done_dependency.id, optional_dependency.id],
                    "depends_on": [done_dependency.title, optional_dependency.title],
                }
            ),
        )
        db.add_all([root, done_dependency, optional_dependency, dependent])
        db.commit()

    await orchestrator._process_children_with_dependencies(
        "optional-dependency-root",
        ["dependent-with-optional-skill-note"],
        SlowPlanningRunner(),
        asyncio.Semaphore(1),
    )

    with SessionLocal() as db:
        dependent = db.get(TaskNode, "dependent-with-optional-skill-note")
        assert dependent is not None
        meta = loads(dependent.node_meta_json, {})

    assert dependent.status == TaskStatus.DONE.value
    assert meta["depends_on_ids"] == ["done-dependency"]
    assert meta["depends_on"] == ["本地阻塞与范围缺失根因审计"]
    assert meta["optional_dependencies_skipped"][0]["task_id"] == "optional-skill-notes"


@pytest.mark.asyncio
async def test_dependency_block_report_preserves_tool_internal_dependency_category():
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=settings.workspace_dir)
    orchestrator = Orchestrator(settings)

    constitution_id = await locked_constitution(orchestrator, "Test dependency category inheritance.")
    root_id = await orchestrator.start_project("Test dependency category inheritance.", constitution_id=constitution_id, auto_run=False)
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.status = TaskStatus.WAITING_CHILDREN.value
        dependency = TaskNode(
            id="tool-internal-dependency",
            parent_id=root.id,
            title="Tool internal dependency",
            description="This dependency hit a tool runtime issue.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            node_meta_json=dumps(
                {
                    "block_report": {
                        "task_id": "tool-internal-dependency",
                        "title": "Tool internal dependency",
                        "status": TaskStatus.BLOCKED.value,
                        "source": "worker.blocked",
                        "category": "tool_internal",
                        "retryable": False,
                        "parent_action": "pause_tool_fix",
                        "reason": "Tool runtime failed; skill lookup text mentions SSO only as a boundary.",
                    }
                }
            ),
            error_message="Tool runtime failed; skill lookup text mentions SSO only as a boundary.",
            constitution_id=root.constitution_id,
            constitution_hash=root.constitution_hash,
            constitution_json=root.constitution_json,
        )
        dependent = TaskNode(
            id="dependent-tool-child",
            parent_id=root.id,
            title="Dependent tool child",
            description="This child needs the dependency first.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            node_meta_json=dumps({"depends_on_ids": ["tool-internal-dependency"]}),
            constitution_id=root.constitution_id,
            constitution_hash=root.constitution_hash,
            constitution_json=root.constitution_json,
        )
        db.add_all([dependency, dependent])
        db.commit()

    await orchestrator._process_children_with_dependencies(root_id, ["dependent-tool-child"], SlowPlanningRunner(), asyncio.Semaphore(1))

    with SessionLocal() as db:
        dependent = db.get(TaskNode, "dependent-tool-child")
        assert dependent is not None
        meta = loads(dependent.node_meta_json, {})

    report = meta["block_report"]
    assert dependent.status == TaskStatus.BLOCKED.value
    assert report["source"] == "dependency.blocked"
    assert report["category"] == "tool_internal"
    assert report["parent_action"] == "pause_tool_fix"
    assert "human_action_required" not in report
    assert report["dependency_reports"][0]["category"] == "tool_internal"


def test_blocked_child_repair_spec_drops_dependencies_on_blocked_siblings():
    settings = get_settings()
    settings = settings.__class__(
        mock_delay_seconds=0,
        workspace_dir=settings.workspace_dir,
        parent_skill_note_task_enabled=False,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(
            id="repair-dependency-parent",
            title="Parent",
            description="Parent with a blocked child.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        blocked = TaskNode(
            id="blocked-sibling",
            parent_id=parent.id,
            title="Blocked sibling",
            description="Blocked original child.",
            depth=1,
            status=TaskStatus.BLOCKED.value,
            node_meta_json=dumps({"block_report": {"category": "tool_internal", "retryable": False, "parent_action": "pause_tool_fix"}}),
        )
        db.add_all([parent, blocked])
        db.flush()
        child_ids = orchestrator._create_children(
            db,
            parent,
            [
                ChildTaskSpec(
                    title="Replacement for blocked sibling",
                    description="Repair or replace the blocked sibling result.",
                    scope=["replacement"],
                    acceptance=["must not wait on the blocked sibling it replaces"],
                    depends_on=["Blocked sibling"],
                    coverage_ids=["blocked-child-repair", "blocked-sibling"],
                    verification_kind="blocked child repair + replacement integration evidence",
                    estimated_leaf=False,
                )
            ],
        )
        db.commit()

    with SessionLocal() as db:
        repair = db.get(TaskNode, child_ids[0])
        assert repair is not None
        meta = loads(repair.node_meta_json, {})

    assert meta["depends_on"] == []
    assert meta["depends_on_ids"] == []
    assert meta["dropped_blocked_repair_dependency_ids"] == ["blocked-sibling"]


def test_run_artifact_preview_is_limited_to_workspace(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(workspace_dir=tmp_path, artifact_preview_chars=5)
    orchestrator = Orchestrator(settings)
    artifact_path = tmp_path / "runs" / "prompt.txt"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("abcdefg", encoding="utf-8")
    outside_path = Path(tempfile.gettempdir()) / "codex-battlefield-outside-artifact.txt"
    outside_path.write_text("outside", encoding="utf-8")

    with SessionLocal() as db:
        task = TaskNode(id="artifact-task", title="Artifact", description="", depth=0, status=TaskStatus.DONE.value)
        db.add(task)
        db.flush()
        run = RunRecord(
            task_id=task.id,
            kind=RunKind.PLANNER.value,
            status=RunStatus.SUCCEEDED.value,
            command="test",
            sandbox="read-only",
            exit_code=0,
            result_json=dumps({"ok": True}),
            prompt_path=str(artifact_path),
            last_message_path=str(outside_path),
        )
        db.add(run)
        db.commit()
        run_id = run.id

        prompt = orchestrator.get_run_artifact(db, run_id, "prompt")
        outside = orchestrator.get_run_artifact(db, run_id, "last_message")

    assert prompt["exists"] is True
    assert prompt["content"] == "abcde"
    assert prompt["truncated"] is True
    assert outside["exists"] is False
    assert outside["content"]


def test_assign_worktree_seeds_files_from_non_git_target(tmp_path: Path):
    target = tmp_path / "target-project"
    target.mkdir()
    (target / "README.md").write_text("project docs", encoding="utf-8")
    (target / "src").mkdir()
    (target / "src" / "main.gd").write_text("extends Node\n", encoding="utf-8")
    (target / ".godot").mkdir()
    (target / ".godot" / "cache.bin").write_text("ignored", encoding="utf-8")
    settings = get_settings()
    settings = settings.__class__(workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="seed-root", title="Root", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value, scope_json=dumps([str(target)]))
        child = TaskNode(id="seed-child", parent_id=root.id, title="Child", description="", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, child])
        db.commit()
        orchestrator._assign_worktree(db, child)
        db.commit()
        worktree_path = Path(child.worktree_path or "")

    assert (worktree_path / "README.md").read_text(encoding="utf-8") == "project docs"
    assert (worktree_path / "src" / "main.gd").read_text(encoding="utf-8") == "extends Node\n"
    assert not (worktree_path / ".godot" / "cache.bin").exists()


def test_worktree_seeds_done_dependency_outputs(tmp_path: Path):
    dependency_worktree = tmp_path / "dependency-worktree"
    dependent_worktree = tmp_path / "dependent-worktree"
    dependency_worktree.mkdir()
    dependent_worktree.mkdir()
    (dependency_worktree / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nrun/main_scene="res://scenes/main.tscn"\n',
        encoding="utf-8",
    )
    settings = get_settings()
    settings = settings.__class__(workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(id="dependency-root", title="Root", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        dependency = TaskNode(
            id="dependency-done",
            parent_id=root.id,
            title="Dependency",
            description="Create base project.",
            depth=1,
            status=TaskStatus.DONE.value,
            worktree_path=str(dependency_worktree),
            result_json=dumps({"worker": {"status": "succeeded", "changed_files": ["project.godot"]}}),
        )
        dependent = TaskNode(
            id="dependent-leaf",
            parent_id=root.id,
            title="Dependent",
            description="Build on base project.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            worktree_path=str(dependent_worktree),
            node_meta_json=dumps({"depends_on_ids": [dependency.id]}),
        )
        db.add_all([root, dependency, dependent])
        db.commit()

        orchestrator._seed_worktree_from_dependencies(db, dependent)
        db.commit()

    project_text = (dependent_worktree / "project.godot").read_text(encoding="utf-8")
    assert 'run/main_scene="res://scenes/main.tscn"' in project_text


def test_worktree_seeds_parent_outputs_for_nested_children(tmp_path: Path):
    parent_worktree = tmp_path / "parent-worktree"
    child_worktree = tmp_path / "child-worktree"
    parent_worktree.mkdir()
    child_worktree.mkdir()
    (parent_worktree / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nrun/main_scene="res://scenes/main/Main.tscn"\n',
        encoding="utf-8",
    )
    (parent_worktree / "scenes" / "main").mkdir(parents=True)
    (parent_worktree / "scenes" / "main" / "Main.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    settings = get_settings()
    settings = settings.__class__(workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        parent = TaskNode(id="nested-parent", title="Parent", description="", depth=1, status=TaskStatus.WAITING_CHILDREN.value, worktree_path=str(parent_worktree))
        child = TaskNode(id="nested-child", parent_id=parent.id, title="Child", description="", depth=2, status=TaskStatus.QUEUED.value, worktree_path=str(child_worktree))
        db.add_all([parent, child])
        db.commit()

        orchestrator._seed_worktree_from_parent(db, child)
        db.commit()

    assert (child_worktree / "project.godot").read_text(encoding="utf-8").startswith("config_version=5")
    assert (child_worktree / "scenes" / "main" / "Main.tscn").read_text(encoding="utf-8") == "[gd_scene format=3]\n"


def test_changed_text_files_are_repaired_before_verifier(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    original = "\u9a8c\u6536\u6807\u51c6\uff1a\u9879\u76ee\u5165\u53e3\u548c\u4e2d\u6587\u8bf4\u660e\u5fc5\u987b\u53ef\u8bfb\u3002"
    broken = original.encode("utf-8").decode("latin1")
    (worktree / "README.md").write_text(broken, encoding="utf-8")
    settings = get_settings()
    settings = settings.__class__(workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    task = TaskNode(id="repair-text", title="Repair text", description="", depth=1, status=TaskStatus.WORKING.value, worktree_path=str(worktree))
    worker_result = WorkerResult(status="succeeded", summary="ok", changed_files=["README.md"])

    repaired = orchestrator._repair_changed_text_files(task, worker_result)

    assert repaired == ["README.md"]
    assert (worktree / "README.md").read_text(encoding="utf-8") == original


def test_question_mark_corrupted_changed_text_files_require_recursive_repair(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "docs").mkdir()
    (worktree / "docs" / "summary.json").write_text(
        '{\n  "title": "????????????",\n  "conclusion": "????????????????"\n}\n',
        encoding="utf-8",
    )
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    task = TaskNode(id="bad-text", title="Bad text", description="", depth=1, status=TaskStatus.WORKING.value, worktree_path=str(worktree))
    worker_result = WorkerResult(status="succeeded", summary="ok", changed_files=["docs/summary.json"])

    issues = orchestrator._worker_text_file_issues(task, worker_result)
    children = orchestrator._text_file_repair_children(task, issues)

    assert issues == [{"path": "docs/summary.json", "reason": "irreversible question-mark text corruption"}]
    assert "UTF-8 中文" in orchestrator._worker_text_file_issue_reason(issues)
    assert children[0].title.endswith("修复文本乱码")
    assert "\\uXXXX" in children[0].description


def test_mojibake_changed_text_files_require_recursive_repair(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "docs").mkdir()
    mojibake_text = (
        "\u9416\u8dfa\u7730\u947a\u509c\u5063\u6960\u5c83\u7609"
        "\u95ab\u6c33\u7e43\u9286\u4e7bource_files"
    )
    (worktree / "docs" / "report.md").write_text(mojibake_text, encoding="utf-8")
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    task = TaskNode(id="bad-mojibake", title="Bad mojibake", description="", depth=1, status=TaskStatus.WORKING.value, worktree_path=str(worktree))
    worker_result = WorkerResult(status="succeeded", summary="ok", changed_files=["docs/report.md"])

    issues = orchestrator._worker_text_file_issues(task, worker_result)
    children = orchestrator._text_file_repair_children(task, issues)

    assert issues == [{"path": "docs/report.md", "reason": "mojibake text corruption"}]
    assert "mojibake" in orchestrator._worker_text_file_issue_reason(issues)
    assert "mojibake" in children[0].description
    assert "楠岃瘉" in children[0].acceptance[0]


def test_waiting_parent_reports_child_progress_without_counting_as_active_agent(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(
        workspace_dir=tmp_path / "workspace",
        max_concurrency=1,
        auto_concurrency_enabled=False,
    )
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        root = TaskNode(
            id="report-root",
            title="Parent reporter",
            description="Parent waits for child convergence.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        waiting_child = TaskNode(
            id="report-waiting-child",
            parent_id=root.id,
            title="Nested parent",
            description="Nested parent is also reporting.",
            depth=1,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        done_child = TaskNode(
            id="report-done-child",
            parent_id=root.id,
            title="Done child",
            description="Already complete.",
            depth=1,
            status=TaskStatus.DONE.value,
        )
        queued_child = TaskNode(
            id="report-queued-child",
            parent_id=root.id,
            title="Queued child",
            description="Waiting for a Codex slot.",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        active_leaf = TaskNode(
            id="report-active-leaf",
            parent_id=waiting_child.id,
            title="Active leaf",
            description="This is the only real Codex CLI agent.",
            depth=2,
            status=TaskStatus.WORKING.value,
        )
        db.add_all([root, waiting_child, done_child, queued_child, active_leaf])
        db.commit()

        tree = orchestrator.get_tree(db, root.id)
        detail = orchestrator.get_detail(db, root.id)

    root_payload = next(task for task in tree["tasks"] if task["id"] == "report-root")
    report = root_payload["node_meta"]["waiting_report"]
    detail_report = detail["task"]["node_meta"]["waiting_report"]

    assert tree["stats"]["active_agents"] == 1
    assert tree["stats"]["active_agent_statuses"] == 1
    assert tree["stats"]["codex_slots_active"] == 0
    assert tree["stats"]["codex_slots_capacity"] == 1
    assert tree["stats"]["waiting_children"] == 2
    assert root_payload["is_active_frontier"] is False
    assert report["role"] == "reporting_parent"
    assert report["direct_total"] == 3
    assert report["direct_done"] == 1
    assert report["subtree_total"] == 4
    assert report["active_agents"] == 1
    assert report["queued_descendants"] == 1
    assert report["active_leaves"][0]["id"] == "report-active-leaf"
    assert detail_report["active_leaves"][0]["title"] == "Active leaf"


def test_get_tree_and_watchdog_count_external_codex_slots_as_active(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        goal_mode=True,
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": index, "task_id": f"external-{index}"} for index in range(13)],
    )

    with SessionLocal() as db:
        root = TaskNode(id="external-slot-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        queued = TaskNode(id="external-slot-child", parent_id=root.id, title="Queued", description="", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, queued])
        db.commit()

        tree = orchestrator.get_tree(db, root.id)
        state = orchestrator.get_watchdog_state(db, root.id)

    assert tree["stats"]["active_agents"] == 13
    assert tree["stats"]["active_agent_statuses"] == 0
    assert tree["stats"]["codex_slots_active"] == 13
    assert state["active_agents"] == 13
    assert state["active_agent_statuses"] == 0
    assert state["codex_slots_active"] == 13
    assert state["recommended_action"] == "observe_running"
    process_slot_check = next(check for check in state["guardian_checks"] if check["key"] == "process_slots")
    assert "13/13" in process_slot_check["detail"]


def test_l4_guardian_treats_target_capacity_without_external_slots_as_ok(monkeypatch, tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path,
        max_concurrency=13,
        auto_concurrency_enabled=True,
        auto_concurrency_min=1,
        auto_concurrency_max=13,
        goal_mode=True,
    )
    orchestrator = Orchestrator(settings)
    monkeypatch.setattr(
        orchestrator,
        "_external_codex_processes",
        lambda max_cache_age_seconds=2.0: [{"pid": index, "task_id": f"active-{index}"} for index in range(13)],
    )

    with SessionLocal() as db:
        root = TaskNode(id="full-slot-root", title="Root", description="Parent.", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        children = [
            TaskNode(
                id=f"active-{index}",
                parent_id=root.id,
                title=f"Active {index}",
                description="",
                depth=1,
                status=TaskStatus.WORKING.value,
            )
            for index in range(13)
        ]
        db.add(root)
        db.add_all(children)
        db.commit()

        state = orchestrator.get_watchdog_state(db, root.id)

    process_slot_check = next(check for check in state["guardian_checks"] if check["key"] == "process_slots")
    assert state["codex_slots_active"] == 13
    assert state["active_agent_statuses"] == 13
    assert process_slot_check["status"] == "ok"


def test_active_agent_diagnostics_ignore_progress_before_current_prompt(tmp_path: Path):
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        codex_idle_timeout_seconds=1800,
    )
    orchestrator = Orchestrator(settings)
    run_dir = settings.workspace_dir / "runs" / "fresh-worker"
    run_dir.mkdir(parents=True)
    stale_log = run_dir / "worker.jsonl"
    stale_log.write_text(
        '{"type":"error","message":"You have hit your usage limit. Try again at 6:24 AM."}\n',
        encoding="utf-8",
    )
    old_time = time.time() - 7200
    os.utime(stale_log, (old_time, old_time))
    prompt = run_dir / "worker-prompt.txt"
    prompt.write_text("fresh prompt", encoding="utf-8")

    with SessionLocal() as db:
        task = TaskNode(
            id="fresh-worker",
            title="Fresh worker",
            description="New run should not inherit stale markers.",
            depth=0,
            status=TaskStatus.WORKING.value,
        )
        db.add(task)
        db.commit()

        state = orchestrator.get_watchdog_state(db, task.id)

    diagnostic = state["active_agent_diagnostics"][0]
    assert diagnostic["task_id"] == "fresh-worker"
    assert diagnostic["severity"] == "running"
    assert diagnostic["latest_progress_path"] is None
    assert diagnostic["markers"] == []
    assert diagnostic["tail_summary"] == ""


def test_get_tree_restores_missing_root_from_state_snapshot(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)

    with SessionLocal() as db:
        constitution = TaskNode(
            id="snapshot-root",
            title="Snapshot Root",
            description="Recover this tree from snapshot.",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
            node_meta_json=dumps({"runner_mode": "real"}),
            acceptance_json=dumps(["Root acceptance."]),
        )
        child = TaskNode(
            id="snapshot-child",
            parent_id="snapshot-root",
            title="Snapshot Child",
            description="Recovered child.",
            depth=1,
            status=TaskStatus.QUEUED.value,
            acceptance_json=dumps(["Child acceptance."]),
        )
        db.add_all([constitution, child])
        db.commit()
        assert orchestrator._write_state_snapshot(db, "snapshot-root", "test.snapshot") is not None
        db.delete(db.get(TaskNode, "snapshot-root"))
        db.commit()

        tree = orchestrator.get_tree(db, "snapshot-root")
        restored_root = db.get(TaskNode, "snapshot-root")
        restored_child = db.get(TaskNode, "snapshot-child")

    assert tree["stats"]["total"] == 2
    assert restored_root is not None
    assert restored_child is not None
    assert restored_root.status == TaskStatus.WAITING_CHILDREN.value
    assert loads(restored_root.node_meta_json, {})["restored_from_snapshot"] is True
    assert any(event["type"] == "tree.snapshot_restored" for event in tree["events"])


@pytest.mark.asyncio
async def test_watchdog_state_restores_hibernating_root_from_snapshot(tmp_path: Path):
    settings = get_settings()
    settings = settings.__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    resume_at = datetime.now(timezone.utc) + timedelta(hours=1)

    with SessionLocal() as db:
        root = TaskNode(
            id="snapshot-hibernating-root",
            title="Snapshot Hibernating Root",
            description="Recover hibernation from snapshot.",
            depth=0,
            status=TaskStatus.HIBERNATING.value,
            node_meta_json=dumps(
                {
                    "runner_mode": "real",
                    "hibernation": {
                        "owner_id": "snapshot-hibernating-root",
                        "previous_status": TaskStatus.QUEUED.value,
                        "resume_at": resume_at.isoformat(),
                        "reason": "wait for refresh",
                        "status": "scheduled",
                    },
                }
            ),
        )
        db.add(root)
        db.commit()
        assert orchestrator._write_state_snapshot(db, root.id, "test.hibernate_snapshot") is not None
        db.delete(db.get(TaskNode, root.id))
        db.commit()

        state = orchestrator.get_watchdog_state(db, root_id="snapshot-hibernating-root")
        restored_root = db.get(TaskNode, "snapshot-hibernating-root")

    assert restored_root is not None
    assert restored_root.status == TaskStatus.HIBERNATING.value
    assert state["root_id"] == "snapshot-hibernating-root"
    assert state["selection"] == "requested"
    assert state["recommended_action"] == "wait_for_resume_at"
    assert "snapshot-hibernating-root" in orchestrator._auto_resume_tasks

    scheduled = list(orchestrator._auto_resume_tasks.values())
    for task in scheduled:
        task.cancel()
    await asyncio.gather(*scheduled, return_exceptions=True)
