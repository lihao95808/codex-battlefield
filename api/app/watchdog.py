"""看门狗的**根节点选择、休眠状态与原因文案**部分。

搬移自 `orchestrator.Orchestrator`。判据是 `scripts/audit_orchestrator_movable_mass.py`
的闭包实测（`--closure`），而不是方法组的表面行数：

- **可搬**：入口集 `_select_watchdog_root` / `_latest_root_id` / `_resume_status_from_hibernation` /
  `_retry_after_seconds_from_reports` / `_resource_auto_hibernate_reason` / `_is_unreadable_reason` /
  `_fallback_hibernation_reason` / `_normalize_watchdog_reason` /
  `_hibernation_reason_is_codex_resource_wait` 实测闭包 **13 个方法 / 115 行**，不触达任何不可搬方法。
  它们只依赖 `settings` 上的标量配置，改成显式参数后可以脱离 `Orchestrator` 直接单测。
- **不可搬，且原因已实测**：
  - `get_watchdog_state`(341 行)、`_l4_guardian_state`、`_watchdog_hook_state`(112 行)
    要 `db` / `_subtree` / 序列化 / 事件写入，是 orchestrator 的消费者。
  - `_can_hibernate_task` 自己只有 4 行，但它调 `_child_block_report` —— 实测这一条边会把
    **整个阻塞分类簇**（`_block_category` 119 行、`_normalize_block_report` 80 行、
    `_build_block_report` 71 行、`_dependency_report_context` 50 行 …）拖进闭包，规模从 115 行
    膨胀到 610 行以上。因此它与阻塞分类同属 W4g，**不能**在这一步抽出来。
- `_watchdog_interval_minutes` / `_watchdog_state_snapshot_due` 已各自是 1 行量级的取数，
  搬走再加委托等于零收益，故留在 orchestrator。

依赖方向：本模块只依赖 `models` 与 `utils`，**不得**导入 `orchestrator`。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import budget
from .models import TERMINAL_STATUSES, TaskNode, TaskStatus, utcnow
from .utils import coerce_utc, loads, repair_mojibake_text

if TYPE_CHECKING:
    from .config import Settings


def latest_root_id(tasks: Sequence[TaskNode]) -> str | None:
    roots = [task for task in tasks if task.parent_id is None]
    if not roots:
        return None
    terminal_values = {status.value for status in TERMINAL_STATUSES}
    active_roots = [task for task in roots if task.status not in terminal_values]
    candidates = active_roots or roots
    non_debug_candidates = [task for task in candidates if "debug" not in task.id.lower()]
    if non_debug_candidates:
        candidates = non_debug_candidates
    fallback = datetime.min.replace(tzinfo=timezone.utc)
    return max(candidates, key=lambda task: coerce_utc(task.created_at) or fallback).id


def select_watchdog_root(tasks: Sequence[TaskNode], requested_root_id: str | None) -> tuple[str | None, str]:
    requested = (
        next((task for task in tasks if task.id == requested_root_id and task.parent_id is None), None)
        if requested_root_id
        else None
    )
    if requested_root_id:
        if requested is None:
            return None, "requested_missing"
        if requested.status in {status.value for status in TERMINAL_STATUSES}:
            return requested.id, "requested_terminal"
        return requested.id, "requested"
    discovered = latest_root_id(tasks)
    if discovered is None:
        return None, "none"
    return discovered, "auto_latest"


def resume_status_from_hibernation(hibernation: dict[str, Any]) -> str:
    previous_status = str(hibernation.get("previous_status") or "")
    if previous_status == TaskStatus.WAITING_CHILDREN.value:
        return TaskStatus.WAITING_CHILDREN.value
    return TaskStatus.QUEUED.value


def retry_after_seconds_from_reports(reports: Sequence[dict[str, Any]], fallback_seconds: int) -> int:
    seconds: list[int] = []
    for report in reports:
        for key in ("retry_after_seconds", "quota_window_seconds"):
            try:
                value = int(float(report.get(key)))
            except (TypeError, ValueError):
                continue
            if value > 0:
                seconds.append(value)
    fallback = max(60, int(fallback_seconds))
    return min(max(max(seconds, default=fallback), 60), 604_800)


def resource_auto_hibernate_reason(reports: Sequence[dict[str, Any]], fallback_seconds: int) -> str:
    titles = [str(report.get("title") or report.get("task_id") or "unknown") for report in reports[:3]]
    suffix = " 等" if len(reports) > 3 else ""
    joined = "、".join(titles) if titles else "未知节点"
    retry_after_seconds = retry_after_seconds_from_reports(reports, fallback_seconds)
    retry_hours = retry_after_seconds / 3600
    if retry_after_seconds >= 3600:
        wait_text = f"约 {retry_hours:.1f} 小时"
    elif retry_after_seconds >= 60:
        wait_text = f"约 {retry_after_seconds // 60} 分钟"
    else:
        wait_text = f"{retry_after_seconds} 秒"
    return (
        "检测到可重试的 Codex 资源/流量阻塞，自动保存状态并休眠，"
        f"建议等待 {wait_text} 后自启动。阻塞节点：{joined}{suffix}。"
    )


def is_unreadable_reason(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    question_count = stripped.count("?")
    if question_count < 4:
        return False
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", stripped))
    return not has_cjk and question_count / max(len(stripped), 1) >= 0.35


def fallback_hibernation_reason(used: int, work_limit: int, reserve: int) -> str:
    if work_limit > 0 and used >= work_limit:
        return (
            f"达到本轮 Codex agent 工作启动额度 {used}/{work_limit}，已保存任务树状态并休眠；"
            f"额外保留 {reserve} 次预算给自动恢复钩子，等待 Codex/token 池刷新后自动恢复。"
        )
    return "已保存任务树状态并休眠，等待 Codex/token 池刷新后自动恢复。"


def normalize_watchdog_reason(reason: str, used: int, work_limit: int, reserve: int) -> str:
    readable = repair_mojibake_text(str(reason or "").strip())
    if not is_unreadable_reason(readable):
        return readable
    return fallback_hibernation_reason(used, work_limit, reserve)


def hibernation_reason_is_codex_resource_wait(reason: str) -> bool:
    text = repair_mojibake_text(str(reason or "")).lower()
    if "machine safety guard" in text:
        return False
    return any(
        token in text
        for token in (
            "codex",
            "quota",
            "token",
            "usage limit",
            "rate limit",
            "resource_block",
            "resource block",
            "retry-after",
            "retry after",
            "限额",
            "额度",
            "流量",
            "资源",
        )
    )


def watchdog_interval_minutes(settings: Settings) -> int:
    return max(1, int(settings.watchdog_interval_minutes))


def api_restart_flag_path(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir) / "restart_api_when_idle.flag"


def api_restart_drain_flag_path(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir) / "restart_api_drain_when_safe.flag"


def codex_health_summary_text(value: Any) -> str:
    code = str(value or "")
    labels = {
        "codex_command_missing": "找不到 codex 命令，无法自动恢复真实 Codex CLI 任务。",
        "codex_login_ok_no_direct_quota_api": "Codex 已登录；Codex CLI 当前没有直接查询 5 小时额度/刷新时间的公开接口。",
        "codex_login_resource_backoff": "Codex 登录检查出现额度/限流退避信号，暂不自动恢复。",
        "codex_login_status_failed": "Codex 登录状态不可用，暂不自动恢复。",
        "codex_doctor_resource_backoff": "Codex doctor 出现额度/限流退避信号，暂不自动恢复。",
        "codex_login_ok_doctor_timeout_warning": "Codex 已登录；doctor 超时，仅作为连通性告警，不拦截自动恢复。",
        "codex_login_ok_doctor_warning": "Codex 已登录；doctor 报告告警，仅作为连通性提示，不拦截自动恢复。",
        "codex_runtime_retry_after_observed": "Codex runtime reported a retry-after window; L4 will preserve state and slow scheduling until the observed reset time.",
        "codex_runtime_backoff_observed_without_reset": "Codex runtime reported a quota/backoff signal, but no exact reset time was exposed by the CLI output.",
        "codex_runtime_no_fresh_quota_signal": "No fresh Codex quota signal was found in recent runtime logs; L4 is using normal scheduling signals.",
        "codex_health_parse_failed": "Codex health snapshot was unreadable; L4 will repair it on the next watchdog pass.",
        "codex_quota_reset_window_elapsed": "The last observed Codex retry-after window has elapsed; L4 may resume normal scheduling if no fresh quota signal appears.",
    }
    return labels.get(code, repair_mojibake_text(code))


def codex_health_state(workspace_dir: str | Path) -> dict[str, Any]:
    health_path = Path(workspace_dir) / "codex_health.json"
    state = {
        "codex_health_path": str(health_path),
        "codex_health_exists": health_path.exists(),
        "codex_health_status": "unknown",
        "codex_health_checked_at": None,
        "codex_health_summary": "尚未执行恢复前 Codex 预检。",
        "codex_quota_observability": "codex_cli_has_no_direct_quota_or_5h_reset_api",
        "codex_quota_retry_after_seconds": None,
        "codex_quota_reset_at": None,
        "codex_quota_retry_after_source": "",
        "codex_quota_signal": "",
        "codex_quota_source": "",
    }
    if not health_path.exists():
        return state
    try:
        data = loads(health_path.read_text(encoding="utf-8-sig"), {})
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        state["codex_health_status"] = "unreadable"
        state["codex_health_summary"] = f"Codex 预检状态文件读取失败：{exc}"
        return state
    if not isinstance(data, dict):
        state["codex_health_status"] = "invalid"
        state["codex_health_summary"] = "Codex 预检状态文件不是 JSON 对象。"
        return state
    checked_at = data.get("checked_at")
    parsed_checked_at = None
    if checked_at:
        try:
            parsed_checked_at = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        except ValueError:
            parsed_checked_at = None
    reset_at = data.get("quota_reset_at")
    parsed_reset_at = None
    if reset_at:
        try:
            parsed_reset_at = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00"))
        except ValueError:
            parsed_reset_at = None
    retry_after_raw = data.get("quota_retry_after_seconds")
    try:
        retry_after_seconds = int(retry_after_raw) if retry_after_raw is not None else None
    except (TypeError, ValueError):
        retry_after_seconds = None
    health_status = str(data.get("status") or "unknown")
    health_summary = codex_health_summary_text(data.get("summary"))[:1200]
    parsed_reset_at_utc = coerce_utc(parsed_reset_at)
    if health_status == "resource_block" and parsed_reset_at_utc is not None:
        remaining = int((parsed_reset_at_utc - utcnow()).total_seconds())
        if remaining <= 0:
            health_status = "warning"
            health_summary = codex_health_summary_text("codex_quota_reset_window_elapsed")[:1200]
            retry_after_seconds = 0
            data["quota_observability"] = "retry_after_elapsed_from_codex_output"
        else:
            retry_after_seconds = max(1, min(remaining, 604_800))
        parsed_reset_at = parsed_reset_at_utc
    state.update(
        {
            "codex_health_status": health_status,
            "codex_health_checked_at": parsed_checked_at,
            "codex_health_summary": health_summary,
            "codex_quota_observability": str(
                data.get("quota_observability") or "codex_cli_has_no_direct_quota_or_5h_reset_api"
            ),
            "codex_quota_retry_after_seconds": retry_after_seconds,
            "codex_quota_reset_at": parsed_reset_at,
            "codex_quota_retry_after_source": str(data.get("quota_retry_after_source") or ""),
            "codex_quota_signal": repair_mojibake_text(str(data.get("quota_signal") or ""))[:1200],
            "codex_quota_source": str(data.get("quota_source") or ""),
        }
    )
    return state


def watchdog_hook_state(root_dir: str | Path, settings: Settings, root_id: str | None) -> dict[str, Any]:
    root_dir = Path(root_dir)
    watchdog_script = root_dir / "scripts" / "battlefield_watchdog.ps1"
    register_script = root_dir / "scripts" / "register_battlefield_watchdog_task.ps1"
    task_name = "CodexBattlefieldWatchdog"
    logon_task_name = f"{task_name}-Logon"
    interval_minutes = watchdog_interval_minutes(settings)
    resume_after_seconds = settings.agent_burst_resume_after_seconds
    scheduled_task_path: Path | None = None
    logon_task_path: Path | None = None
    system_root = os.getenv("SystemRoot") or os.getenv("WINDIR")
    if system_root:
        scheduled_task_path = Path(system_root) / "System32" / "Tasks" / task_name
        logon_task_path = Path(system_root) / "System32" / "Tasks" / logon_task_name
    startup_hook_path: Path | None = None
    appdata = os.getenv("APPDATA")
    if appdata:
        startup_hook_path = (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / f"{logon_task_name}.cmd"
        )

    scheduled_task_exists = bool(scheduled_task_path and scheduled_task_path.exists())
    logon_task_exists = bool(logon_task_path and logon_task_path.exists())
    startup_hook_exists = bool(startup_hook_path and startup_hook_path.exists())
    if logon_task_exists:
        startup_hook_mode = "scheduled_task"
    elif startup_hook_exists:
        startup_hook_mode = "startup_folder"
    else:
        startup_hook_mode = "not_installed"

    health_state = codex_health_state(settings.workspace_dir)
    root_part = f' -RootId "{root_id}"' if root_id else ""
    goal_part = " -GoalMode" if settings.goal_mode else ""
    codex_home_part = f' -CodexHome "{settings.codex_home}"' if settings.codex_home else ""
    profile_part = f' -CodexProfile "{settings.codex_profile}"' if settings.codex_profile else ""
    agent_burst_limit = budget.agent_burst_limit(settings)
    agent_burst_reserve = budget.agent_burst_effective_reserve(settings)
    run_command = (
        f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{watchdog_script}"'
        f"{root_part}"
        f"{goal_part}"
        f"{codex_home_part}"
        f"{profile_part}"
        f" -MaxConcurrency {settings.max_concurrency}"
        f" -AgentBurstLimit {agent_burst_limit}"
        f" -AgentBurstReserve {agent_burst_reserve}"
        f" -ResumeAfterSeconds {resume_after_seconds}"
        " -CodexPreflightTimeoutSeconds 25"
        f" -AutoConcurrencyQueueThreshold {settings.auto_concurrency_queue_threshold}"
        f" -FuelDeadlineHour {settings.auto_concurrency_fuel_deadline_hour}"
        f" -FuelTimezoneOffsetHours {settings.auto_concurrency_fuel_timezone_offset_hours}"
        f" -FuelSprintWindowMinutes {settings.auto_concurrency_fuel_sprint_window_minutes}"
    )
    install_command = (
        f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{register_script}"'
        f"{root_part}"
        f"{goal_part}"
        f"{codex_home_part}"
        f"{profile_part}"
        f" -IntervalMinutes {interval_minutes}"
        f" -MaxConcurrency {settings.max_concurrency}"
        f" -AgentBurstLimit {agent_burst_limit}"
        f" -AgentBurstReserve {agent_burst_reserve}"
        f" -ResumeAfterSeconds {resume_after_seconds}"
        " -CodexPreflightTimeoutSeconds 25"
        f" -AutoConcurrencyQueueThreshold {settings.auto_concurrency_queue_threshold}"
        f" -FuelDeadlineHour {settings.auto_concurrency_fuel_deadline_hour}"
        f" -FuelTimezoneOffsetHours {settings.auto_concurrency_fuel_timezone_offset_hours}"
        f" -FuelSprintWindowMinutes {settings.auto_concurrency_fuel_sprint_window_minutes}"
        " -WithLogonTrigger"
    )
    if settings.goal_mode:
        reserve_summary = (
            "Goal 长跑模式已开启：不使用本轮启动预算上限；"
            f"固定 {settings.max_concurrency} 并发普通速度，保留空闲看门狗、资源阻塞自动休眠与本地自启动巡检。"
        )
    elif agent_burst_limit > 0:
        reserve_summary = (
            f"本轮最多启动 {budget.agent_burst_work_limit(settings)} 个真实 Codex agent；"
            f"额外保留 {agent_burst_reserve} 次预算给自动恢复后的第一批工作。"
        )
    else:
        reserve_summary = "未启用 agent 启动预算上限；watchdog 本身只做本地检查，不消耗模型 token。"
    restart_flag_path = api_restart_flag_path(settings.workspace_dir)
    restart_drain_flag_path = api_restart_drain_flag_path(settings.workspace_dir)
    return {
        "watchdog_log_path": str(Path(settings.workspace_dir) / "watchdog.log"),
        "watchdog_run_command": run_command,
        "watchdog_install_command": install_command,
        "watchdog_interval_minutes": interval_minutes,
        "api_restart_flag_path": str(restart_flag_path),
        "api_restart_pending": restart_flag_path.exists(),
        "api_restart_drain_flag_path": str(restart_drain_flag_path),
        "api_restart_drain_armed": restart_drain_flag_path.exists(),
        "startup_hook_path": str(startup_hook_path) if startup_hook_path else None,
        "startup_hook_exists": startup_hook_exists,
        "startup_hook_mode": startup_hook_mode,
        "scheduled_task_path": str(scheduled_task_path) if scheduled_task_path else None,
        "scheduled_task_exists": scheduled_task_exists,
        "logon_task_path": str(logon_task_path) if logon_task_path else None,
        "logon_task_exists": logon_task_exists,
        "scheduled_task_name": task_name,
        "logon_task_name": logon_task_name,
        "token_reserve_summary": reserve_summary,
        **health_state,
    }
