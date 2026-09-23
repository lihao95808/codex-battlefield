from __future__ import annotations

import importlib.util
import json
import sqlite3
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_local_watchdog_state_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "local_watchdog_state.py"
    spec = importlib.util.spec_from_file_location("local_watchdog_state_under_test", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_tree_db(db_path: Path, *, child_count: int = 20) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            "create table task_nodes ("
            "id text primary key, parent_id text, status text, depth integer, "
            "updated_at text, node_meta_json text)"
        )
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "insert into task_nodes values (?, ?, ?, ?, ?, ?)",
            ("local-root", None, "waiting_children", 0, now, "{}"),
        )
        for index in range(child_count):
            connection.execute(
                "insert into task_nodes values (?, ?, ?, ?, ?, ?)",
                (f"local-child-{index}", "local-root", "queued", 1, now, "{}"),
            )
        connection.commit()
    finally:
        connection.close()


def _state_for(db_path: Path, *, slots_active: int = 0, capacity: int = 13, queue_threshold: int = 6):
    module = _load_local_watchdog_state_module()
    return module.build_state(
        Namespace(
            db=str(db_path),
            root_id="local-root",
            slots_active=slots_active,
            capacity=capacity,
            goal_mode=True,
            queue_threshold=queue_threshold,
            fuel_deadline_hour=20,
            fuel_timezone_offset_hours=8.0,
            fuel_sprint_window_minutes=120,
        )
    )


def test_local_watchdog_state_holds_topoff_during_future_quota_reset(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_tree_db(db_path)
    reset_at = datetime.now(timezone.utc) + timedelta(hours=1)
    (tmp_path / "codex_health.json").write_text(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "resource_block",
                "summary": "codex_runtime_retry_after_observed",
                "quota_observability": "retry_after_observed_from_codex_output",
                "quota_retry_after_seconds": 3600,
                "quota_reset_at": reset_at.isoformat(),
                "quota_retry_after_source": "codex_output",
                "quota_source": "run_log:runs/node-a/worker.jsonl",
            }
        ),
        encoding="utf-8",
    )

    state = _state_for(db_path)

    assert state["active_resource_pressure"] is True
    assert state["recommended_action"] == "wait_for_quota_reset"
    assert state["l4_fuel_target"] == 1
    assert state["l4_fuel_phase"] == "quota_wait"
    assert state["codex_quota_retry_after_seconds"] > 0


def test_local_watchdog_state_resumes_topoff_after_elapsed_quota_reset(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_tree_db(db_path)
    reset_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    (tmp_path / "codex_health.json").write_text(
        json.dumps(
            {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "status": "resource_block",
                "summary": "codex_runtime_retry_after_observed",
                "quota_observability": "retry_after_observed_from_codex_output",
                "quota_retry_after_seconds": 3600,
                "quota_reset_at": reset_at.isoformat(),
                "quota_retry_after_source": "codex_output",
                "quota_source": "run_log:runs/node-a/worker.jsonl",
            }
        ),
        encoding="utf-8",
    )

    state = _state_for(db_path)

    assert state["active_resource_pressure"] is False
    assert state["recommended_action"] == "top_off"
    assert state["l4_fuel_target"] == 13
    assert state["codex_health_status"] == "warning"
    assert state["codex_quota_retry_after_seconds"] == 0


def test_local_watchdog_state_reports_backlog_fuel_when_capacity_is_full(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_tree_db(db_path, child_count=90)

    state = _state_for(db_path, slots_active=13)

    assert state["recommended_action"] == "observe_running"
    assert state["queued_work"] == 90
    assert state["l4_fuel_target"] == 13
    assert state["l4_fuel_mode"] == "high_throughput"
    assert state["l4_fuel_phase"] == "backlog_sprint"
    assert state["l4_fuel_score"] > 0
    assert state["l4_fuel_queue_pressure"] == 1.0
    assert state["l4_fuel_frontier_pressure"] == 1.0
    assert state["l4_fuel_deadline_local"]


def test_local_watchdog_state_topoff_uses_real_slots_not_stale_statuses(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_tree_db(db_path, child_count=20)
    connection = sqlite3.connect(str(db_path))
    try:
        now = datetime.now(timezone.utc).isoformat()
        for index in range(14):
            connection.execute(
                "insert into task_nodes values (?, ?, ?, ?, ?, ?)",
                (f"stale-active-{index}", "local-root", "working", 1, now, "{}"),
            )
        connection.commit()
    finally:
        connection.close()

    state = _state_for(db_path, slots_active=5, capacity=13)

    assert state["codex_slots_active"] == 5
    assert state["active_agent_statuses"] == 14
    assert state["active_agents"] == 14
    assert state["recommended_action"] == "top_off"
    assert state["l4_fuel_target"] == 13


def test_local_watchdog_state_fuel_score_drives_midrange_target(monkeypatch, tmp_path: Path):
    module = _load_local_watchdog_state_module()
    monkeypatch.setattr(module, "utcnow", lambda: datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))
    db_path = tmp_path / "battlefield.sqlite3"
    _create_tree_db(db_path, child_count=5)

    state = module.build_state(
        Namespace(
            db=str(db_path),
            root_id="local-root",
            slots_active=0,
            capacity=13,
            goal_mode=True,
            queue_threshold=6,
            fuel_deadline_hour=20,
            fuel_timezone_offset_hours=0,
            fuel_sprint_window_minutes=120,
        )
    )

    assert state["recommended_action"] == "top_off"
    assert state["l4_fuel_target"] == 3
    assert state["l4_fuel_desired"] == 3
    assert state["l4_fuel_recommended_target"] == 3
    assert state["l4_fuel_open_frontier"] == 5
    assert state["l4_fuel_time_pressure"] == 0.0
