from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest


def _load_anchor_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "git_snapshot_anchor.py"
    spec = importlib.util.spec_from_file_location("git_snapshot_anchor_under_test", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test Snapshotter")
    _git(repo, "config", "user.email", "snapshotter@example.local")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for snapshot anchor tests")
def test_git_snapshot_anchor_writes_restore_ready_ledger(tmp_path: Path):
    module = _load_anchor_module()
    battlefield = tmp_path / "battlefield"
    workspace = battlefield / "workspace"
    target = tmp_path / "target"
    root_id = "root-anchor-test"
    checkpoint_id = "20260618T010203000000Z"
    checkpoint_dir = workspace / "full_checkpoints" / root_id / checkpoint_id
    target_copy = checkpoint_dir / "target"
    state_snapshot = workspace / "state_snapshots" / root_id / "latest.json"
    db_copy = checkpoint_dir / "battlefield.sqlite3"

    _init_repo(battlefield)
    (battlefield / ".gitignore").write_text(
        "workspace/full_checkpoints/\n"
        "workspace/state_snapshots/\n"
        "!workspace/checkpoint_ledger/\n"
        "!workspace/checkpoint_ledger/**\n",
        encoding="utf-8",
    )
    (battlefield / "README.md").write_text("battlefield\n", encoding="utf-8")
    _git(battlefield, "add", ".")
    _git(battlefield, "commit", "-m", "Initial battlefield")

    _init_repo(target)
    (target / "project.godot").write_text("[application]\nconfig/name=\"Probe\"\n", encoding="utf-8")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "Initial target")
    (target / "gameplay.gd").write_text("extends Node\n", encoding="utf-8")

    target_copy.mkdir(parents=True)
    (target_copy / "project.godot").write_text("[application]\nconfig/name=\"Probe copy\"\n", encoding="utf-8")
    state_snapshot.parent.mkdir(parents=True)
    state_snapshot.write_text(json.dumps({"root_id": root_id}), encoding="utf-8")
    db_copy.write_text("sqlite copy placeholder", encoding="utf-8")
    manifest = {
        "root_id": root_id,
        "reason": "watchdog.hourly",
        "created_at": "2026-06-18T01:02:03+00:00",
        "database_copy": str(db_copy),
        "state_snapshot": str(state_snapshot),
        "target_source": str(target),
        "target_copy": str(target_copy),
        "target_copied": True,
    }
    manifest_path = checkpoint_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    module.ROOT_DIR = battlefield
    module.WORKSPACE_DIR = workspace

    result = module.anchor_checkpoint(
        Namespace(
            root_id=root_id,
            manifest=str(manifest_path),
            target_path=None,
            init_git=True,
            commit=True,
            tag=True,
            push=False,
            remote="origin",
        )
    )

    ledger_path = workspace / "checkpoint_ledger" / root_id / f"{checkpoint_id}.json"
    latest_path = workspace / "checkpoint_ledger" / root_id / "latest.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    latest = json.loads(latest_path.read_text(encoding="utf-8"))

    assert ledger["version_id"] == f"{root_id}-{checkpoint_id}"
    assert latest["checkpoint_id"] == checkpoint_id
    assert ledger["state_snapshot"] == str(state_snapshot)
    assert ledger["target_source"] == str(target.resolve())
    assert ledger["target_copy"] == str(target_copy.resolve())
    assert ledger["target_git_commit_after"] == _git(target, "rev-parse", "--verify", "HEAD")
    assert ledger["target_git_tag"].endswith("-target")
    assert ledger["battlefield_git_tag"].endswith("-battlefield")
    assert ledger["restore"]["database_copy"] == str(db_copy.resolve())
    assert ledger["restore"]["target_restore_ref"] == ledger["target_git_tag"]
    assert result["battlefield_git_commit_after"] == _git(battlefield, "rev-parse", "--verify", "HEAD")
    assert _git(battlefield, "status", "--porcelain") == ""
    assert _git(target, "status", "--porcelain") == ""
    assert _git(battlefield, "rev-parse", "--verify", f"refs/tags/{ledger['battlefield_git_tag']}")
    assert _git(target, "rev-parse", "--verify", f"refs/tags/{ledger['target_git_tag']}")

    head_before = _git(battlefield, "rev-parse", "--verify", "HEAD")
    commit_count_before = _git(battlefield, "rev-list", "--count", "HEAD")
    second = module.anchor_checkpoint(
        Namespace(
            root_id=root_id,
            manifest=str(manifest_path),
            target_path=None,
            init_git=True,
            commit=True,
            tag=True,
            push=False,
            remote="origin",
        )
    )

    assert second["already_anchored"] is True
    assert second["ledger_path"] == str(ledger_path)
    assert _git(battlefield, "rev-parse", "--verify", "HEAD") == head_before
    assert _git(battlefield, "rev-list", "--count", "HEAD") == commit_count_before
    assert _git(battlefield, "status", "--porcelain") == ""
