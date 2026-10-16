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
import os
import shutil
import sqlite3
import sys
import time
import uuid
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


# ---------- 原子写（P5-6）----------
#
# 为什么这一层必须有原子写：`latest.json` 是**还原锚点**。进程在写它的中途被杀
# （断电、taskkill、容器被杀）会留下一个半截文件，而半截 JSON 解不出来 ——
# 于是"表被清空"那条通路（`_restore_snapshot_if_missing`）就拿不到东西了。
# **这正是最坏的方向**：崩溃恰好发生在最需要还原的时候。
#
# 而且它是**静默**的：`write_text` 不报错，读者那边也只看到 None。

_SNAPSHOT_BACKUP_SUFFIX = ".backup"


def atomic_write_json(path: Path, payload: Any) -> None:
    """把 JSON **原子**写到 `path`，并保留上一份好的作为 `.backup`。

    三步，缺一不可：

    1. 先把**当前那份**（如果解得出 JSON）复制成 `.backup` —— 它是崩溃时的退路；
    2. 写一个**每个写者唯一**的临时文件（进程号 + 随机段），再 `os.replace` 进去。
       用共享的 `<name>.tmp` 会让两个写者互相踩（实测过：一方 replace 之后另一方
       Errno 13，而调用方的 except 把它静默吞掉，于是那次更新完全没发生）；
    3. 窗口内的 `os.replace` 重试：Windows 上目标文件被读者打开时会失败。

    `os.replace` 保证读者只会看到**旧内容或新内容**，不会看到半截。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    # 步骤 1：留退路。解不出 JSON 的旧文件不值得备份（备份它等于备份一个坏锚点）。
    if path.is_file():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        else:
            try:
                shutil.copy2(path, path.with_name(path.name + _SNAPSHOT_BACKUP_SUFFIX))
            except OSError:
                pass

    # 步骤 2：唯一临时名 + 换名。
    unique = f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp_path = path.with_name(path.name + unique)
    try:
        tmp_path.write_text(text, encoding="utf-8")
        _replace_with_retry(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 10) -> None:
    """换名带**有限次**重试（Windows 上读者会短暂持有目标文件）。

    有限而不是无限：一个卡住的读者不该把写入方永久堵死。约 0.3 秒后放弃并抛错，
    由调用方决定怎么处理（快照这条通路是"尽力而为"，写不上就下次再写）。
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.03 * (attempt + 1))
    assert last is not None
    raise last


def atomic_write_text(path: Path, text: str) -> None:
    """纯文本版的原子写（给 `latest.json` 之外的产物用）。

    与 `atomic_write_json` 同一套命名与重试逻辑 —— 两份实现会漂移，而漂移的表现是
    "某个文件偶尔半截"，那是最难查的一类。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp_path = path.with_name(path.name + unique)
    try:
        tmp_path.write_text(text, encoding="utf-8")
        _replace_with_retry(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
    """读还原锚点。**主文件坏了就退回 `.backup`**（P5-6）。

    只读主文件是不够的：进程可能在写它的中途被杀，留下一个解不出来的半截文件。
    那种情况下"表被清空"这条通路就静默失效 —— 而它存在的全部意义就是**在那个时候**
    把树还原回来。

    所以：主文件解不出来时退到 `.backup`（原子写之前留的那份好的），
    并**打印一行 stderr** —— 静默用备份会掩盖"真的丢过一次写"这件事。
    """
    base = Path(workspace_dir) / "state_snapshots" / root_id
    primary = base / "latest.json"
    payload = _read_json_object(primary)
    if payload is not None:
        return payload

    backup = primary.with_name(primary.name + _SNAPSHOT_BACKUP_SUFFIX)
    recovered = _read_json_object(backup)
    if recovered is not None:
        print(
            f"[checkpoint] 还原锚点 {primary} 读不出来，已改用备份 {backup.name} —— "
            "说明上一次快照写在崩溃中被截断了。",
            file=sys.stderr,
        )
    return recovered


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
