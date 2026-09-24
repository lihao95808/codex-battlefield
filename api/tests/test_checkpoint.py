from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app import checkpoint
from app.orchestrator import Orchestrator


def test_sqlite_database_path() -> None:
    assert checkpoint.sqlite_database_path("sqlite:///C:/x/y.db") == Path("C:/x/y.db")
    assert checkpoint.sqlite_database_path("sqlite:///") is None
    assert checkpoint.sqlite_database_path("postgresql://host/db") is None


def test_full_checkpoint_root_composition(tmp_path: Path) -> None:
    assert checkpoint.full_checkpoint_root(tmp_path, "root-1") == tmp_path / "full_checkpoints" / "root-1"


def test_checkpoint_ignore_filters_known_names() -> None:
    names = [".git", "art", ".godot", "node_modules", "main.gd"]
    assert checkpoint.checkpoint_ignore("anything", names) == {".git", ".godot", "node_modules"}


def test_full_checkpoint_artifacts_exist(tmp_path: Path) -> None:
    assert checkpoint.full_checkpoint_artifacts_exist({}) is True
    assert checkpoint.full_checkpoint_artifacts_exist({"database_copy": "  "}) is True

    real = tmp_path / "db.sqlite3"
    real.write_text("x", encoding="utf-8")
    assert checkpoint.full_checkpoint_artifacts_exist({"database_copy": str(real)}) is True
    assert checkpoint.full_checkpoint_artifacts_exist({"database_copy": str(tmp_path / "missing")}) is False
    # 两个都必须在
    assert (
        checkpoint.full_checkpoint_artifacts_exist(
            {"database_copy": str(real), "target_copy": str(tmp_path / "missing")}
        )
        is False
    )


def test_prune_full_checkpoints_keeps_newest_by_name(tmp_path: Path) -> None:
    root = checkpoint.full_checkpoint_root(tmp_path, "root-1")
    for name in ("20260101T000000Z-a", "20260102T000000Z-b", "20260103T000000Z-c"):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
    # 没有 manifest 的目录不参与计数
    (root / "not-a-checkpoint").mkdir()

    checkpoint.prune_full_checkpoints(tmp_path, "root-1", keep=2)
    remaining = sorted(p.name for p in root.iterdir())
    assert remaining == ["20260102T000000Z-b", "20260103T000000Z-c", "not-a-checkpoint"]


def test_read_state_snapshot(tmp_path: Path) -> None:
    assert checkpoint.read_state_snapshot(tmp_path, "root-1") is None

    snapshot_dir = tmp_path / "state_snapshots" / "root-1"
    snapshot_dir.mkdir(parents=True)
    latest = snapshot_dir / "latest.json"
    latest.write_text(json.dumps({"root_id": "root-1"}), encoding="utf-8")
    assert checkpoint.read_state_snapshot(tmp_path, "root-1") == {"root_id": "root-1"}

    latest.write_text("{not json", encoding="utf-8")
    assert checkpoint.read_state_snapshot(tmp_path, "root-1") is None

    latest.write_text("[1, 2]", encoding="utf-8")
    assert checkpoint.read_state_snapshot(tmp_path, "root-1") is None


def test_latest_snapshot_root_id_picks_newest(tmp_path: Path) -> None:
    assert checkpoint.latest_snapshot_root_id(tmp_path) is None

    older = tmp_path / "state_snapshots" / "root-old"
    newer = tmp_path / "state_snapshots" / "root-new"
    for directory in (older, newer):
        directory.mkdir(parents=True)
        (directory / "latest.json").write_text("{}", encoding="utf-8")
    # 让 root-new 的 mtime 明确更新
    stat = (newer / "latest.json").stat()
    os.utime(newer / "latest.json", (stat.st_atime, stat.st_mtime + 10))

    assert checkpoint.latest_snapshot_root_id(tmp_path) == "root-new"


def test_parse_snapshot_datetime() -> None:
    now = datetime.now(timezone.utc)
    assert checkpoint.parse_snapshot_datetime(now) == now
    assert checkpoint.parse_snapshot_datetime(None) is None
    assert checkpoint.parse_snapshot_datetime("") is None
    assert checkpoint.parse_snapshot_datetime("2026-01-02T03:04:05Z") == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )
    assert checkpoint.parse_snapshot_datetime("not-a-date") is None


def test_state_snapshot_due(tmp_path: Path) -> None:
    # interval <= 0 一律到期
    assert checkpoint.state_snapshot_due(tmp_path, "root-1", 0) is True
    # 没有快照一律到期
    assert checkpoint.state_snapshot_due(tmp_path, "root-1", 300) is True

    snapshot_dir = tmp_path / "state_snapshots" / "root-1"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "latest.json").write_text("{}", encoding="utf-8")
    # 刚写过 → 未到期
    assert checkpoint.state_snapshot_due(tmp_path, "root-1", 3600) is False


def test_backup_sqlite_database_copies_content(tmp_path: Path) -> None:
    source = tmp_path / "src.sqlite3"
    connection = sqlite3.connect(str(source))
    connection.execute("CREATE TABLE t (v INTEGER)")
    connection.execute("INSERT INTO t VALUES (7)")
    connection.commit()
    connection.close()

    destination = tmp_path / "out" / "copy.sqlite3"
    checkpoint.backup_sqlite_database(source, destination)
    assert destination.is_file()

    copied = sqlite3.connect(str(destination))
    try:
        assert copied.execute("SELECT v FROM t").fetchone() == (7,)
    finally:
        copied.close()


def test_orchestrator_delegates_match_the_module(tmp_path: Path) -> None:
    """反漂移：搬移后 Orchestrator 的同名方法必须与模块函数一致。"""
    assert Orchestrator._checkpoint_ignore(None, "d", [".git", "keep"]) == checkpoint.checkpoint_ignore(
        "d", [".git", "keep"]
    )
    assert Orchestrator._parse_snapshot_datetime(None, "2026-01-02T03:04:05Z") == checkpoint.parse_snapshot_datetime(
        "2026-01-02T03:04:05Z"
    )
    assert Orchestrator._full_checkpoint_artifacts_exist(None, {}) == checkpoint.full_checkpoint_artifacts_exist({})
