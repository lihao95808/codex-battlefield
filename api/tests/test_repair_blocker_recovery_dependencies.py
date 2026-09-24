from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone


def _create_schema(db_path: Path) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            """
            create table task_nodes(
                id text primary key,
                parent_id text,
                status text,
                title text,
                error_message text,
                node_meta_json text,
                updated_at text,
                depth integer
            )
            """
        )
        connection.execute(
            """
            create table event_records(
                task_id text,
                type text,
                message text,
                payload_json text,
                created_at text
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def _insert_task(connection: sqlite3.Connection, task_id: str, parent_id: str | None, status: str, title: str, meta: dict) -> None:
    connection.execute(
        """
        insert into task_nodes(id, parent_id, status, title, error_message, node_meta_json, updated_at, depth)
        values (?, ?, ?, ?, null, ?, '2026-06-18 00:00:00', ?)
        """,
        (task_id, parent_id, status, title, json.dumps(meta), 0 if parent_id is None else 1),
    )


def test_dependency_block_retargeted_to_superseding_repair_child(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_schema(db_path)
    with sqlite3.connect(str(db_path)) as connection:
        _insert_task(connection, "root", None, "waiting_children", "Root", {})
        _insert_task(
            connection,
            "blocked-dependency",
            "root",
            "blocked",
            "Blocked dependency",
            {"superseded_by_repair_child_ids": ["repair-child"]},
        )
        _insert_task(connection, "repair-child", "root", "waiting_children", "Repair child", {})
        _insert_task(
            connection,
            "dependent",
            "root",
            "blocked",
            "Dependent child",
            {
                "depends_on_ids": ["blocked-dependency"],
                "depends_on": ["Blocked dependency"],
                "block_report": {
                    "source": "dependency.blocked",
                    "category": "tool_internal",
                    "parent_action": "pause_tool_fix",
                    "retryable": False,
                    "dependencies": ["blocked-dependency"],
                },
            },
        )
        connection.commit()

    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_blocker_recovery_dependencies.py"
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--root-id", "root"],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["retargeted"] == ["dependent"]
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        dependent = connection.execute("select status, node_meta_json from task_nodes where id='dependent'").fetchone()
        blocked = connection.execute("select status, node_meta_json from task_nodes where id='blocked-dependency'").fetchone()
        events = connection.execute("select type from event_records order by created_at").fetchall()

    dependent_meta = json.loads(dependent["node_meta_json"])
    blocked_meta = json.loads(blocked["node_meta_json"])
    assert dependent["status"] == "queued"
    assert dependent_meta["depends_on_ids"] == ["repair-child"]
    assert dependent_meta["depends_on"] == ["Repair child"]
    assert "block_report" not in dependent_meta
    assert dependent_meta["previous_dependency_block_report"]["source"] == "dependency.blocked"
    assert blocked["status"] == "blocked"
    assert blocked_meta["superseded_by_repair_child_ids"] == ["repair-child"]
    assert {event["type"] for event in events} == {
        "task.dependency_block_retargeted_to_repair",
        "parent.dependency_block_retargeted_to_repair",
    }


def test_dependency_block_requeued_when_dependency_is_no_longer_bad(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_schema(db_path)
    with sqlite3.connect(str(db_path)) as connection:
        _insert_task(connection, "root", None, "waiting_children", "Root", {})
        _insert_task(connection, "dependency", "root", "queued", "Dependency", {})
        _insert_task(
            connection,
            "dependent",
            "root",
            "blocked",
            "Dependent child",
            {
                "depends_on_ids": ["dependency"],
                "depends_on": ["Dependency"],
                "block_report": {
                    "source": "dependency.blocked",
                    "category": "external",
                    "parent_action": "skill_lookup_recovery",
                    "retryable": False,
                    "reason": "Dependency was blocked earlier by a read-only sandbox.",
                    "dependencies": ["dependency"],
                },
            },
        )
        connection.commit()

    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_blocker_recovery_dependencies.py"
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--root-id", "root"],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["unblocked"] == ["dependent"]
    assert payload["requeued"] == ["dependent"]
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        dependent = connection.execute("select status, node_meta_json from task_nodes where id='dependent'").fetchone()
        events = connection.execute("select type from event_records order by created_at").fetchall()

    dependent_meta = json.loads(dependent["node_meta_json"])
    assert dependent["status"] == "queued"
    assert "block_report" not in dependent_meta
    assert dependent_meta["previous_dependency_block_report"]["source"] == "dependency.blocked"
    assert {event["type"] for event in events} == {"task.dependency_block_unblocked_requeued"}


def test_canceled_optional_skill_lookup_dependency_is_pruned_and_requeued(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_schema(db_path)
    with sqlite3.connect(str(db_path)) as connection:
        _insert_task(connection, "root", None, "waiting_children", "Root", {})
        _insert_task(connection, "done-dependency", "root", "done", "本地阻塞与范围缺失根因审计", {})
        _insert_task(connection, "optional-skill-notes", "root", "canceled", "skill_notes 与工具流程误判机制审计", {})
        _insert_task(
            connection,
            "dependent",
            "root",
            "blocked",
            "阻塞根因报告整合与验证",
            {
                "depends_on_ids": ["done-dependency", "optional-skill-notes"],
                "depends_on": ["本地阻塞与范围缺失根因审计", "skill_notes 与工具流程误判机制审计"],
                "block_report": {
                    "source": "dependency.blocked",
                    "category": "external",
                    "parent_action": "skill_lookup_recovery",
                    "retryable": False,
                    "reason": "optional Skill/KM dependency was canceled",
                    "dependencies": ["optional-skill-notes"],
                },
            },
        )
        connection.commit()

    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_blocker_recovery_dependencies.py"
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--root-id", "root"],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["skipped_optional"] == ["dependent"]
    assert payload["unblocked"] == ["dependent"]
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        dependent = connection.execute("select status, node_meta_json from task_nodes where id='dependent'").fetchone()
        events = connection.execute("select type from event_records order by created_at").fetchall()

    dependent_meta = json.loads(dependent["node_meta_json"])
    assert dependent["status"] == "queued"
    assert dependent_meta["depends_on_ids"] == ["done-dependency"]
    assert dependent_meta["depends_on"] == ["本地阻塞与范围缺失根因审计"]
    assert dependent_meta["optional_dependencies_skipped"][0]["task_id"] == "optional-skill-notes"
    assert "block_report" not in dependent_meta
    assert {event["type"] for event in events} == {
        "task.dependency_block_unblocked_requeued",
        "task.optional_dependency_skipped",
    }


def test_dependency_block_inherits_workspace_sandbox_category(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_schema(db_path)
    reason = "依赖节点未完成：当前会话文件系统为只读且 approval policy=never，写入补丁被拒绝。"
    with sqlite3.connect(str(db_path)) as connection:
        _insert_task(connection, "root", None, "waiting_children", "Root", {})
        _insert_task(
            connection,
            "dependent",
            "root",
            "blocked",
            "Dependent child",
            {
                "block_report": {
                    "source": "dependency.blocked",
                    "category": "external",
                    "parent_action": "skill_lookup_recovery",
                    "retryable": False,
                    "reason": reason,
                    "dependency_reports": [
                        {
                            "source": "worker.blocked",
                            "category": "external",
                            "parent_action": "skill_lookup_recovery",
                            "retryable": False,
                            "reason": reason,
                        }
                    ],
                },
            },
        )
        connection.commit()

    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_blocker_recovery_dependencies.py"
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--root-id", "root"],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["reclassified"] == ["dependent"]
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        dependent = connection.execute("select node_meta_json from task_nodes where id='dependent'").fetchone()

    report = json.loads(dependent["node_meta_json"])["block_report"]
    assert report["category"] == "workspace_sandbox"
    assert report["retryable"] is True
    assert report["parent_action"] == "wait_and_retry"


def test_retryable_block_wait_uses_codex_health_reset_instead_of_fallback(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_schema(db_path)
    reset_at = datetime.now(timezone.utc) + timedelta(minutes=20)
    (tmp_path / "codex_health.json").write_text(
        json.dumps(
            {
                "status": "resource_block",
                "quota_reset_at": reset_at.isoformat(),
                "quota_retry_after_seconds": 1200,
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(str(db_path)) as connection:
        _insert_task(connection, "root", None, "waiting_children", "Root", {})
        _insert_task(
            connection,
            "rate-limited",
            "root",
            "blocked",
            "Rate limited child",
            {
                "block_report": {
                    "source": "worker.blocked",
                    "category": "rate_limit",
                    "parent_action": "wait_and_retry",
                    "retryable": True,
                    "retry_after_seconds": 18000,
                    "quota_window_seconds": 18000,
                    "retry_after_source": "fallback_resource_window",
                    "reported_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    "reason": "资源/流量类阻塞，可等待后重试：You've hit your usage limit. Try again at 11:25 AM.",
                },
            },
        )
        connection.commit()

    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_retryable_block_reports.py"
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--root-id", "root", "--retry-after-seconds", "300"],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["adjusted_wait"] == ["rate-limited"]
    assert payload["requeued"] == []
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("select status, node_meta_json from task_nodes where id='rate-limited'").fetchone()

    meta = json.loads(row["node_meta_json"])
    report = meta["block_report"]
    assert row["status"] == "blocked"
    assert report["retry_after_source"] == "codex_health_runtime_observation"
    assert 60 <= int(report["retry_after_seconds"]) <= 1200
    assert report["codex_health_reset_at"].startswith(reset_at.replace(microsecond=0).isoformat()[:16])


def test_retryable_block_requeues_when_codex_health_reset_elapsed(tmp_path: Path):
    db_path = tmp_path / "battlefield.sqlite3"
    _create_schema(db_path)
    reset_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    (tmp_path / "codex_health.json").write_text(
        json.dumps(
            {
                "status": "resource_block",
                "quota_reset_at": reset_at.isoformat(),
                "quota_retry_after_seconds": 18000,
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(str(db_path)) as connection:
        _insert_task(connection, "root", None, "waiting_children", "Root", {})
        _insert_task(
            connection,
            "elapsed-rate-limited",
            "root",
            "blocked",
            "Elapsed rate limited child",
            {
                "block_report": {
                    "source": "worker.blocked",
                    "category": "rate_limit",
                    "parent_action": "wait_and_retry",
                    "retryable": True,
                    "retry_after_seconds": 18000,
                    "quota_window_seconds": 18000,
                    "retry_after_source": "fallback_resource_window",
                    "reported_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "资源/流量类阻塞，可等待后重试：You've hit your usage limit. Try again at 11:25 AM.",
                },
            },
        )
        connection.commit()

    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_retryable_block_reports.py"
    result = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--root-id", "root", "--retry-after-seconds", "300"],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(result.stdout)
    assert payload["adjusted_wait"] == ["elapsed-rate-limited"]
    assert payload["requeued"] == ["elapsed-rate-limited"]
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("select status, node_meta_json from task_nodes where id='elapsed-rate-limited'").fetchone()

    meta = json.loads(row["node_meta_json"])
    report = meta["block_report"]
    assert row["status"] == "queued"
    assert report["retry_after_seconds"] == 60
    assert report["codex_health_reset_elapsed"] is True
