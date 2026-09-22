"""检查点与状态快照的**文件系统 / 解析**部分。

搬移自 `orchestrator.Orchestrator`。**只搬了可搬的那一半**，这一点很重要：

- **可搬**：路径推导、sqlite 备份、目录清理、快照读取、时间解析——它们只依赖
  `workspace_dir` / `database_url` 这类标量配置，因此改为**显式参数**传入，
  比原先的 `self.settings` 更清楚地声明了依赖。
- **不可搬**：`_write_state_snapshot` / `_write_full_checkpoint` / `_restore_snapshot_if_missing` /
  `_write_checkpoint_ledger` / `_full_checkpoint_stats`。它们不是内聚的一片，而是**大量子系统的消费者**
  （`_sync_live_external_agent_statuses`、`_subtree`、`_blocker_visibility_counts`、
  `_codex_slots_active`、`_auto_concurrency_decision`、`_reported_codex_slots_capacity`、
  `_constitution_for_tasks`、`serialize_task` / `serialize_constitution` / `serialize_event`）。
  把它们塞进本模块等于要传半个 orchestrator —— 那是架构重写，不是搬移。故留在原处。

依赖方向：本模块只依赖 `models.utcnow` 与标准库，**不得**导入 `orchestrator`。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import utcnow

CHECKPOINT_IGNORED_NAMES = {".git", ".godot", ".import", "__pycache__", ".codex_pycache", "node_modules", ".venv", "venv"}


def full_checkpoint_root(workspace_dir: str | Path, root_id: str) -> Path:
    return Path(workspace_dir) / "full_checkpoints" / root_id


def sqlite_database_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw_path = database_url[len(prefix) :]
    if not raw_path:
        return None
    return Path(raw_path)


def backup_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(str(source))
    try:
        destination_connection = sqlite3.connect(str(destination))
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    except sqlite3.Error:
        shutil.copy2(source, destination)
    finally:
        source_connection.close()
    journal_path = destination.with_name(f"{destination.name}-journal")
    journal_path.unlink(missing_ok=True)


def checkpoint_ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in CHECKPOINT_IGNORED_NAMES}


def full_checkpoint_artifacts_exist(manifest: dict[str, Any]) -> bool:
    artifact_paths: list[Path] = []
    for key in ("database_copy", "target_copy"):
        value = manifest.get(key)
        if isinstance(value, str) and value.strip():
            artifact_paths.append(Path(value))
    if not artifact_paths:
        return True
    return all(path.exists() for path in artifact_paths)


def prune_full_checkpoints(workspace_dir: str | Path, root_id: str, keep: int) -> None:
    checkpoint_root = full_checkpoint_root(workspace_dir, root_id)
    if not checkpoint_root.is_dir():
        return
    checkpoint_dirs = sorted(
        [path for path in checkpoint_root.iterdir() if path.is_dir() and (path / "manifest.json").is_file()],
        key=lambda path: path.name,
        reverse=True,
    )
    for stale in checkpoint_dirs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def read_state_snapshot(workspace_dir: str | Path, root_id: str) -> dict[str, Any] | None:
    path = Path(workspace_dir) / "state_snapshots" / root_id / "latest.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def latest_snapshot_root_id(workspace_dir: str | Path) -> str | None:
    snapshots_dir = Path(workspace_dir) / "state_snapshots"
    if not snapshots_dir.is_dir():
        return None
    candidates: list[tuple[float, str]] = []
    for latest in snapshots_dir.glob("*/latest.json"):
        try:
            candidates.append((latest.stat().st_mtime, latest.parent.name))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates)[1]


def parse_snapshot_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def state_snapshot_due(workspace_dir: str | Path, root_id: str, interval_seconds: int) -> bool:
    interval = max(0, int(interval_seconds))
    if interval <= 0:
        return True
    latest_path = Path(workspace_dir) / "state_snapshots" / root_id / "latest.json"
    try:
        latest_mtime = latest_path.stat().st_mtime
    except OSError:
        return True
    latest_at = datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
    return (utcnow() - latest_at).total_seconds() >= interval
