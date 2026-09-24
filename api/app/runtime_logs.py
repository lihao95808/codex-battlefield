"""Agent 日志扫描、Codex 进程识别、内存探测与资源压力判定。

搬移自 `orchestrator.Orchestrator`。判据是 `scripts/audit_orchestrator_movable_mass.py`
的 `--closure` + `--state` 实测：

- 入口集 `_agent_log_markers` / `_latest_meaningful_agent_log_time` / `_resource_pressure_state` /
  `_available_memory_mb` 闭包 = 17 方法 / 206 行；加上以 `Orchestrator.X` **类级**调用的
  `_runtime_marker_candidate_line`(33) 以及进程命令行识别与内存探测（6 方法 / 98 行）
  ⇒ 本模块共 **22 个方法 / 约 278 行**。
- **实测 100% 零实例状态**（`--state` 输出 `methods with zero instance-state use: 17/17` 与 `6/6`），
  因此这一簇可以干净地改写成自由函数——这正是混合方案里「状态无关的那一半」。

搬移中修正的两处**工具盲区**（不是行为变更）：
1. 普查只追 `self.X` 形式的调用边，因此 `_runtime_marker_candidate_line`（被以
   `Orchestrator._runtime_marker_candidate_line(...)` 调用）**不在闭包里**却被依赖。
   本模块把它收了进来；同时在 PLAN-STATUS §5 记录该盲区，避免以后漏搬。
2. `_runtime_marker_candidate_line` 内部引用 `Orchestrator._has_timeout_marker` /
   `_has_resource_limit_marker`，这里改为直接调用 `runtime_markers` 的同名函数（语义相同，
   那两者本来就只是一行委托）。

常量归属：`ACTIVE_AGENT_STATUSES` 与 `CODEX_LOG_TIMESTAMP_RE` 只被本簇与 orchestrator 共用，
现定义在本模块，orchestrator 反向 import，**调用点零改动**。

依赖方向：本模块只依赖 `models` / `utils` / `runtime_markers` / `blockers` 与标准库，**不得**导入 `orchestrator`。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import blockers, runtime_markers
from .codex_signals import is_local_skill_failure
from .models import EventRecord, RunKind, TaskNode, TaskStatus, utcnow
from .runner import is_meaningful_codex_activity_line
from .utils import coerce_utc, loads, repair_mojibake_text

if TYPE_CHECKING:
    from .config import Settings


ACTIVE_AGENT_STATUSES = {
    TaskStatus.PLANNING.value,
    TaskStatus.DECOMPOSING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.WORKING.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.ACCEPTING.value,
}

CODEX_LOG_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")


def seconds_since(value: datetime | None) -> int:
    value = coerce_utc(value)
    if value is None:
        return 0
    return max(0, int((utcnow() - value).total_seconds()))


def agent_phase_for_status(status: str) -> str:
    if status in {TaskStatus.PLANNING.value, TaskStatus.DECOMPOSING.value}:
        return RunKind.PLANNER.value
    if status == TaskStatus.VERIFYING.value:
        return RunKind.VERIFIER.value
    if status == TaskStatus.ACCEPTING.value:
        return RunKind.ACCEPTANCE.value
    return RunKind.WORKER.value


def codex_log_line_timestamp(line: str) -> datetime | None:
    match = CODEX_LOG_TIMESTAMP_RE.search(line)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None


def file_mtime_utc(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def latest_meaningful_agent_log_time(log_path: Path) -> datetime | None:
    if not log_path.is_file():
        return None
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 262144))
            raw = handle.read()
    except OSError:
        return None
    text = repair_mojibake_text(raw.decode("utf-8", errors="replace"))
    lines = text.splitlines()
    meaningful_indexes = [index for index, line in enumerate(lines) if is_meaningful_codex_activity_line(line)]
    if not meaningful_indexes:
        return None
    last_meaningful_index = meaningful_indexes[-1]
    timestamp = codex_log_line_timestamp(lines[last_meaningful_index])
    if timestamp is not None:
        return timestamp
    for line in lines[last_meaningful_index + 1 : min(len(lines), last_meaningful_index + 12)]:
        timestamp = codex_log_line_timestamp(line)
        if timestamp is not None:
            return timestamp
    for line in reversed(lines[:last_meaningful_index]):
        timestamp = codex_log_line_timestamp(line)
        if timestamp is not None:
            return timestamp
    return file_mtime_utc(log_path)


def runtime_marker_candidate_lines(text: str) -> list[str]:
    candidates: list[str] = []
    for line in text.splitlines():
        candidate = runtime_marker_candidate_line(line)
        if candidate:
            candidates.append(candidate)
    return candidates


def runtime_marker_candidate_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        if not isinstance(payload, dict):
            return ""
        event_type = str(payload.get("type") or "").lower()
        if event_type in {
            "error",
            "response.failed",
            "run.failed",
            "session.error",
            "thread.error",
            "turn.failed",
        }:
            return stripped
        if "item" in payload:
            return ""
        if any(key in payload for key in ("error", "message", "severity", "level")):
            return stripped
        return ""
    if CODEX_LOG_TIMESTAMP_RE.search(stripped):
        return stripped
    if re.search(r"(?i)\b(?:error|fatal|http\s+status|rate limit|quota|usage limit|too many requests|retry-after|retry after)\b", stripped):
        return stripped
    if runtime_markers.has_timeout_marker(stripped.lower()) or runtime_markers.has_resource_limit_marker(stripped.lower()):
        return stripped
    return ""


def agent_log_markers(log_path: Path, *, since: datetime | None = None) -> tuple[list[str], str]:
    if not log_path.is_file():
        return [], ""
    since = coerce_utc(since)
    if since is not None:
        latest_log_at = latest_meaningful_agent_log_time(log_path) or file_mtime_utc(log_path)
        latest_log_at = coerce_utc(latest_log_at)
        if latest_log_at is None or latest_log_at < since:
            return [], ""
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - 8192)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return [], ""
    if start > 0:
        newline_index = raw.find(b"\n")
        raw = raw[newline_index + 1 :] if newline_index >= 0 else b""
    text = repair_mojibake_text(raw.decode("utf-8", errors="replace"))
    marker_text = "\n".join(runtime_marker_candidate_lines(text))
    normalized = marker_text.lower()
    markers: list[str] = []
    if "reconnecting" in normalized or "stream disconnected" in normalized:
        markers.append("reconnecting")
    if "falling back to http" in normalized:
        markers.append("http_fallback")
    if runtime_markers.has_timeout_marker(normalized):
        markers.append("timeout")
    if runtime_markers.has_resource_limit_marker(normalized):
        markers.append("resource_limit")
    meaningful_lines = [line.strip() for line in text.splitlines() if is_meaningful_codex_activity_line(line)]
    summary = " ".join(meaningful_lines[-4:])
    return list(dict.fromkeys(markers)), summary[-700:]


def task_id_from_codex_command_line(command_line: str) -> str:
    normalized = command_line.replace("/", "\\")
    match = re.search(r"workspace\\runs\\([^\\\s\"]+)", normalized, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"workspace\\worktrees\\([^\\\s\"]+)", normalized, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def agent_status_from_codex_command_line(command_line: str) -> TaskStatus | None:
    normalized = command_line.replace("/", "\\").lower()
    if "planner-decision.schema.json" in normalized or "\\planner-last-message.json" in normalized:
        return TaskStatus.PLANNING
    if "worker-result.schema.json" in normalized or "\\worker-last-message.json" in normalized:
        return TaskStatus.WORKING
    if "verifier-result.schema.json" in normalized or "\\verifier-last-message.json" in normalized:
        return TaskStatus.VERIFYING
    if "acceptance-result.schema.json" in normalized or "\\acceptance-last-message.json" in normalized:
        return TaskStatus.ACCEPTING
    return None


def is_battlefield_agent_codex_command_line(command_line: str) -> bool:
    normalized = command_line.replace("/", "\\").lower()
    if "--output-last-message" not in normalized:
        return False
    return agent_status_from_codex_command_line(command_line) is not None


def available_memory_mb() -> float | None:
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return round(float(status.ullAvailPhys) / 1024 / 1024, 1)
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(float(pages * page_size) / 1024 / 1024, 1)
    except Exception:
        return None


def machine_safety_pressure(
    settings: Settings,
    memory_probe: Callable[[], float | None] = available_memory_mb,
) -> tuple[bool, dict[str, Any]]:
    """`memory_probe` 显式传入，是为了保住 orchestrator 侧的 `_available_memory_mb` 测试接缝。

    现有测试在实例上 monkeypatch `_available_memory_mb`；若这里直接调用模块函数，
    那层接缝会消失、测试要改。把探针作为参数传入后，接缝位置不变、行为也不变。
    """
    threshold = max(0, int(settings.machine_safety_min_available_memory_mb))
    if threshold <= 0:
        return False, {"enabled": False}
    available = memory_probe()
    payload: dict[str, Any] = {
        "enabled": True,
        "available_memory_mb": available,
        "safe_min_available_memory_mb": threshold,
    }
    if available is None:
        payload["probe_failed"] = True
        return False, payload
    return available < threshold, payload


def is_resource_pressure_block_report(report: dict[str, Any]) -> bool:
    normalized = blockers.normalize_block_report(report)
    return bool(normalized.get("retryable")) and str(normalized.get("category") or "") in blockers.RESOURCE_PRESSURE_BLOCK_CATEGORIES


def active_resource_pressure_from_logs(tasks: list[TaskNode], settings: Settings) -> tuple[bool, str]:
    cooldown = max(0, int(settings.auto_concurrency_resource_cooldown_seconds))
    for task in tasks:
        if task.status not in ACTIVE_AGENT_STATUSES:
            continue
        phase = agent_phase_for_status(task.status)
        log_path = Path(settings.workspace_dir) / "runs" / task.id / f"{phase}.jsonl"
        markers, _tail_summary = agent_log_markers(log_path)
        if "resource_limit" not in markers:
            continue
        marker_at = file_mtime_utc(log_path) or task.updated_at
        if cooldown and seconds_since(marker_at) > cooldown:
            continue
        return True, f"活跃节点 {task.id} 日志检测到额度/限流信号。"
    return False, ""


def resource_pressure_state(db: Session, tasks: list[TaskNode], settings: Settings) -> tuple[bool, str]:
    active_pressure, active_reason = active_resource_pressure_from_logs(tasks, settings)
    if active_pressure:
        return True, active_reason
    task_ids = [task.id for task in tasks]
    for task in tasks:
        if task.status != TaskStatus.BLOCKED.value:
            continue
        report = blockers.child_block_report(task, settings)
        if is_resource_pressure_block_report(report):
            report_text = " ".join(
                repair_mojibake_text(str(report.get(key) or ""))
                for key in ("reason", "summary", "category", "source", "parent_action")
            )
            if is_local_skill_failure(report_text):
                continue
            return True, f"节点 {task.id} 上报可重试阻塞：{report.get('category') or report.get('source') or 'resource'}"
    if not task_ids:
        return False, ""
    cooldown = max(0, int(settings.auto_concurrency_resource_cooldown_seconds))
    recent_events = list(
        db.scalars(
            select(EventRecord)
            .where(EventRecord.task_id.in_(task_ids))
            .order_by(EventRecord.created_at.desc())
            .limit(120)
        ).all()
    )
    for event in recent_events:
        if cooldown and seconds_since(event.created_at) > cooldown:
            continue
        payload = loads(event.payload_json, {})
        message = repair_mojibake_text(event.message).lower()
        reason = repair_mojibake_text(str(payload.get("reason") or "")).lower()
        if event.type == "budget.guard_hibernate":
            return True, "启动预算保护触发过自动休眠。"
        if event.type == "parent.child_block_reports":
            reports = payload.get("reports") if isinstance(payload, dict) else []
            retryable_resource_reports = [
                report
                for report in reports or []
                if isinstance(report, dict)
                and is_resource_pressure_block_report(report)
                and not is_local_skill_failure(
                    " ".join(repair_mojibake_text(str(report.get(key) or "")) for key in ("reason", "summary", "category", "source"))
                )
            ]
            if retryable_resource_reports:
                return True, "父节点收到过可重试资源类阻塞上报。"
        if event.type == "tree.hibernate" and "资源/流量阻塞" in (message + reason) and not is_local_skill_failure(message + reason):
            return True, "任务树因可重试资源/流量阻塞休眠过。"
    return False, ""
