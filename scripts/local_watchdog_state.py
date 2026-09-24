from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ACTIVE_FRONTIER_STATUSES = {
    "planned",
    "planning",
    "decomposing",
    "queued",
    "waiting_children",
    "running",
    "working",
    "verifying",
    "accepting",
    "repair",
}

ACTIVE_AGENT_STATUSES = {"planning", "decomposing", "running", "working", "verifying", "accepting"}
TERMINAL_OR_STOPPED_STATUSES = {"done", "blocked", "failed", "paused", "hibernating", "canceled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read a minimal Codex Battlefield watchdog state directly from SQLite.")
    parser.add_argument("--db", required=True, help="Path to workspace/battlefield.sqlite3.")
    parser.add_argument("--root-id", default="", help="Preferred root id. If omitted or missing, choose the latest active root.")
    parser.add_argument("--slots-active", type=int, default=0, help="Observed real Codex process slots.")
    parser.add_argument("--capacity", type=int, default=1, help="Configured Codex slot capacity.")
    parser.add_argument("--goal-mode", action="store_true", help="Report goal mode enabled.")
    parser.add_argument("--queue-threshold", type=int, default=6, help="Queued work threshold for L4 fuel decisions.")
    parser.add_argument("--fuel-deadline-hour", type=int, default=20, help="Local hour by which quota should be consumed.")
    parser.add_argument("--fuel-timezone-offset-hours", type=float, default=8.0, help="Local timezone offset for fuel deadline.")
    parser.add_argument("--fuel-sprint-window-minutes", type=int, default=120, help="Minutes before deadline that count as sprint window.")
    return parser.parse_args()


def rows_for_database(db_path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute(
                "select id, parent_id, status, depth, updated_at, node_meta_json from task_nodes "
                "order by depth, updated_at"
            )
        )
    finally:
        connection.close()


def select_root(rows: list[sqlite3.Row], requested_root_id: str) -> tuple[str, str]:
    by_id = {str(row["id"]): row for row in rows}
    if requested_root_id and requested_root_id in by_id:
        return requested_root_id, "local_sqlite_requested"
    roots = [row for row in rows if not row["parent_id"]]
    active_roots = [row for row in roots if str(row["status"]) not in TERMINAL_OR_STOPPED_STATUSES]
    candidates = active_roots or roots
    if not candidates:
        return requested_root_id, "local_sqlite_missing"
    candidates.sort(key=lambda row: str(row["updated_at"] or ""), reverse=True)
    return str(candidates[0]["id"]), "local_sqlite_auto_latest"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_codex_health(db_path: Path) -> dict[str, Any]:
    health_path = db_path.parent / "codex_health.json"
    state: dict[str, Any] = {
        "codex_health_path": str(health_path),
        "codex_health_exists": health_path.is_file(),
        "codex_health_status": "unknown",
        "codex_health_checked_at": None,
        "codex_health_summary": "local sqlite fallback did not find codex_health.json",
        "codex_quota_observability": "codex_cli_has_no_direct_quota_or_5h_reset_api",
        "codex_quota_retry_after_seconds": None,
        "codex_quota_reset_at": None,
        "codex_quota_retry_after_source": "",
        "codex_quota_signal": "",
        "codex_quota_source": "",
    }
    if not health_path.is_file():
        return state
    try:
        payload = json.loads(health_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        state["codex_health_status"] = "unreadable"
        state["codex_health_summary"] = f"local sqlite fallback could not read codex_health.json: {exc}"
        return state
    if not isinstance(payload, dict):
        state["codex_health_status"] = "invalid"
        state["codex_health_summary"] = "local sqlite fallback found invalid codex_health.json"
        return state

    checked_at = parse_datetime(payload.get("checked_at"))
    reset_at = parse_datetime(payload.get("quota_reset_at"))
    retry_after_raw = payload.get("quota_retry_after_seconds")
    try:
        retry_after_seconds = int(retry_after_raw) if retry_after_raw is not None else None
    except (TypeError, ValueError):
        retry_after_seconds = None

    status = str(payload.get("status") or "unknown")
    observability = str(payload.get("quota_observability") or "codex_cli_has_no_direct_quota_or_5h_reset_api")
    if status == "resource_block" and reset_at is not None:
        remaining = int((reset_at - utcnow()).total_seconds())
        if remaining <= 0:
            status = "warning"
            observability = "retry_after_elapsed_from_codex_output"
            retry_after_seconds = 0
        else:
            retry_after_seconds = max(1, remaining)

    state.update(
        {
            "codex_health_status": status,
            "codex_health_checked_at": checked_at.isoformat() if checked_at else None,
            "codex_health_summary": str(payload.get("summary") or ""),
            "codex_quota_observability": observability,
            "codex_quota_retry_after_seconds": retry_after_seconds,
            "codex_quota_reset_at": reset_at.isoformat() if reset_at else None,
            "codex_quota_retry_after_source": str(payload.get("quota_retry_after_source") or ""),
            "codex_quota_signal": str(payload.get("quota_signal") or "")[:1200],
            "codex_quota_source": str(payload.get("quota_source") or ""),
        }
    )
    return state


def subtree(rows: list[sqlite3.Row], root_id: str) -> list[sqlite3.Row]:
    by_id = {str(row["id"]): row for row in rows}
    children: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        parent_id = row["parent_id"]
        if parent_id:
            children[str(parent_id)].append(str(row["id"]))
    selected: list[sqlite3.Row] = []
    stack = [root_id]
    while stack:
        task_id = stack.pop()
        row = by_id.get(task_id)
        if row is None:
            continue
        selected.append(row)
        stack.extend(children.get(task_id, []))
    return selected


def backlog_target(
    *,
    lower: int,
    upper: int,
    queue_threshold: int,
    queued_work: int,
    active_frontier: int,
    active_agents: int,
) -> int:
    runnable_frontier = max(0, active_frontier - active_agents)
    if queued_work <= 0 or runnable_frontier <= 0 or upper <= lower:
        return lower
    runnable_ceiling = max(lower, min(upper, active_frontier))
    if active_frontier >= upper and queued_work >= queue_threshold:
        return upper
    if queued_work >= queue_threshold * max(2, upper):
        return upper
    if queued_work >= queue_threshold * 3:
        return runnable_ceiling
    if queued_work >= queue_threshold * 2:
        return min(runnable_ceiling, max(lower + 2, math.ceil(upper * 0.75)))
    if queued_work >= queue_threshold:
        return min(runnable_ceiling, max(lower + 1, math.ceil((lower + upper) / 2)))
    if queued_work >= max(2, queue_threshold // 2):
        return min(runnable_ceiling, lower + 1)
    return lower


def fuel_state(
    *,
    queued_work: int,
    active_frontier: int,
    active_agents: int,
    lower: int = 1,
    upper: int,
    queue_threshold: int,
    codex_health: dict[str, Any],
    deadline_hour: int,
    timezone_offset_hours: float,
    sprint_window_minutes: int,
) -> dict[str, Any]:
    upper = max(1, int(upper))
    lower = max(1, min(int(lower), upper))
    queue_threshold = max(1, int(queue_threshold))
    deadline_hour = max(0, min(23, int(deadline_hour)))
    timezone_offset_hours = max(-12.0, min(14.0, float(timezone_offset_hours)))
    sprint_window = max(1, int(sprint_window_minutes))
    local_tz = timezone(timedelta(hours=timezone_offset_hours))
    local_now = utcnow().astimezone(local_tz)
    deadline = local_now.replace(hour=deadline_hour, minute=0, second=0, microsecond=0)
    deadline_elapsed = local_now >= deadline
    next_deadline = deadline + timedelta(days=1) if deadline_elapsed else deadline
    minutes_to_deadline = 0 if deadline_elapsed else max(0, int((deadline - local_now).total_seconds() // 60))
    queue_pressure = min(1.0, queued_work / max(1, queue_threshold * upper))
    open_frontier = max(0, active_frontier - active_agents)
    frontier_pressure = min(1.0, open_frontier / max(1, upper))
    time_pressure = 0.0
    if minutes_to_deadline <= sprint_window:
        time_pressure = 1.0 - (minutes_to_deadline / sprint_window)
    score = round(max(0.0, min(1.0, queue_pressure * 0.45 + frontier_pressure * 0.35 + time_pressure * 0.20)), 3)

    quota_reset_at = parse_datetime(codex_health.get("codex_quota_reset_at"))
    quota_minutes_to_reset: int | None = None
    if quota_reset_at and quota_reset_at > utcnow():
        quota_minutes_to_reset = max(0, int((quota_reset_at - utcnow()).total_seconds() // 60))
    quota_observability = str(codex_health.get("codex_quota_observability") or "codex_cli_has_no_direct_quota_or_5h_reset_api")

    basis = "queue_pressure"
    if queued_work <= 0 or open_frontier <= 0:
        phase = "coast"
    elif quota_minutes_to_reset is not None and codex_health.get("codex_health_status") == "resource_block":
        phase = "quota_wait"
        basis = "observed_quota_reset"
        score = 0.0
    elif queue_pressure >= 1.0 or frontier_pressure >= 1.0:
        phase = "backlog_sprint"
    elif deadline_elapsed and (queue_pressure >= 0.15 or frontier_pressure >= 0.25):
        phase = "utilization_sprint"
        basis = "operator_utilization_deadline_elapsed"
    elif minutes_to_deadline <= sprint_window and (queue_pressure >= 0.25 or frontier_pressure >= 0.5):
        phase = "utilization_sprint"
        basis = "operator_utilization_deadline"
    elif queue_pressure >= 0.5:
        phase = "steady_pull"
    else:
        phase = "coast"

    recommended_target = lower
    if queued_work > 0 and open_frontier > 0 and upper > lower:
        if phase in {"backlog_sprint", "utilization_sprint"}:
            score_target = upper
        else:
            score_target = lower + math.ceil((upper - lower) * score)
        recommended_target = max(lower, min(upper, active_frontier, score_target))

    return {
        "phase": phase,
        "score": score,
        "deadline_hour": deadline_hour,
        "deadline_local": deadline.isoformat(),
        "next_deadline_local": next_deadline.isoformat(),
        "deadline_elapsed": int(deadline_elapsed),
        "local_now": local_now.isoformat(),
        "minutes_to_deadline": minutes_to_deadline,
        "sprint_window_minutes": sprint_window,
        "basis": basis,
        "queue_pressure": round(queue_pressure, 3),
        "frontier_pressure": round(frontier_pressure, 3),
        "time_pressure": round(time_pressure, 3),
        "open_frontier": open_frontier,
        "recommended_target": recommended_target,
        "quota_observability": quota_observability,
        "quota_reset_at": quota_reset_at.isoformat() if quota_reset_at else None,
        "quota_minutes_to_reset": quota_minutes_to_reset,
    }


def build_state(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db)
    codex_health = read_codex_health(db_path)
    if not db_path.is_file():
        return {
            "root_id": args.root_id,
            "requested_root_id": args.root_id,
            "selection": "local_sqlite_missing_database",
            "root_status": "",
            "active_agents": 0,
            "codex_slots_active": max(0, args.slots_active),
            "codex_slots_capacity": max(1, args.capacity),
            "active_frontier": 0,
            "hibernating": 0,
            "resume_due": False,
            "should_resume": False,
            "recommended_action": "idle",
            **codex_health,
        }
    rows = rows_for_database(db_path)
    root_id, selection = select_root(rows, args.root_id)
    tasks = subtree(rows, root_id) if root_id else []
    counts = Counter(str(row["status"]) for row in tasks)
    parent_ids = {str(row["parent_id"]) for row in tasks if row["parent_id"]}
    active_frontier = sum(
        1 for row in tasks if str(row["id"]) not in parent_ids and str(row["status"]) in ACTIVE_FRONTIER_STATUSES
    )
    queued_work = counts["planned"] + counts["queued"] + counts["repair"]
    active_agent_statuses = sum(1 for row in tasks if str(row["status"]) in ACTIVE_AGENT_STATUSES)
    root = next((row for row in tasks if str(row["id"]) == root_id), None)
    slots_active = max(0, int(args.slots_active))
    active_agents = max(active_agent_statuses, slots_active)
    capacity = max(1, int(args.capacity))
    quota_reset_at = parse_datetime(codex_health.get("codex_quota_reset_at"))
    quota_waiting = codex_health.get("codex_health_status") == "resource_block" and (
        quota_reset_at is None or quota_reset_at > utcnow()
    )
    active_resource_pressure = bool(quota_waiting)
    active_resource_pressure_reason = ""
    if quota_waiting:
        reset_text = quota_reset_at.isoformat() if quota_reset_at else "unknown"
        active_resource_pressure_reason = f"local fallback observed Codex quota backoff; reset_at={reset_text}"

    queue_threshold = max(1, int(args.queue_threshold))
    lower = 1
    fuel = fuel_state(
        queued_work=queued_work,
        active_frontier=active_frontier,
        active_agents=slots_active,
        lower=lower,
        upper=capacity,
        queue_threshold=queue_threshold,
        codex_health=codex_health,
        deadline_hour=args.fuel_deadline_hour,
        timezone_offset_hours=args.fuel_timezone_offset_hours,
        sprint_window_minutes=args.fuel_sprint_window_minutes,
    )
    if active_resource_pressure:
        desired = lower
    else:
        backlog_desired = backlog_target(
            lower=lower,
            upper=capacity,
            queue_threshold=queue_threshold,
            queued_work=queued_work,
            active_frontier=active_frontier,
            active_agents=slots_active,
        )
        desired = max(backlog_desired, int(fuel["recommended_target"]))
        if fuel["phase"] in {"utilization_sprint", "backlog_sprint"} and desired > lower:
            desired = capacity
    target = max(lower, min(capacity, int(desired)))

    action = "observe_running" if active_agents > 0 else "observe"
    if active_resource_pressure:
        action = "observe_running" if active_agents > 0 else "wait_for_quota_reset"
    elif slots_active < target and active_frontier > slots_active:
        action = "top_off"

    if active_resource_pressure:
        l4_mode = "resource_cooldown"
    elif target >= capacity and capacity > lower:
        l4_mode = "utilization_sprint" if fuel["phase"] == "utilization_sprint" else "high_throughput"
    elif target > lower:
        l4_mode = "balanced"
    else:
        l4_mode = "observe"

    if active_resource_pressure:
        l4_reason = active_resource_pressure_reason
    elif action == "top_off":
        l4_reason = (
            f"local fallback top-off slots={slots_active}/{capacity}, target={target}, "
            f"queued={queued_work}, frontier={active_frontier}, phase={fuel['phase']}"
        )
    else:
        l4_reason = (
            f"local fallback slots={slots_active}/{capacity}, target={target}, queued={queued_work}, "
            f"frontier={active_frontier}, phase={fuel['phase']}"
        )

    return {
        "root_id": root_id,
        "requested_root_id": args.root_id,
        "selection": selection,
        "root_status": str(root["status"]) if root is not None else "",
        "active_agents": active_agents,
        "active_agent_statuses": active_agent_statuses,
        "codex_slots_active": slots_active,
        "codex_slots_capacity": capacity,
        "queued_work": queued_work,
        "queue_threshold": queue_threshold,
        "active_frontier": active_frontier,
        "hibernating": counts["hibernating"],
        "resume_due": False,
        "should_resume": False,
        "reason": "local sqlite fallback after API state timeout",
        "goal_mode": bool(args.goal_mode),
        "budget_guard_active": False,
        "active_resource_pressure": active_resource_pressure,
        "active_resource_pressure_reason": active_resource_pressure_reason,
        "agent_burst_used": 0,
        "agent_burst_limit": 0,
        "agent_burst_reserve": 0,
        "agent_burst_work_limit": 0,
        "agent_burst_remaining": 0,
        "recommended_action": action,
        "l4_fuel_mode": l4_mode,
        "l4_fuel_target": target,
        "l4_fuel_desired": desired,
        "l4_fuel_current_capacity": capacity,
        "l4_fuel_phase": fuel["phase"],
        "l4_fuel_score": fuel["score"],
        "l4_fuel_basis": fuel["basis"] if not active_resource_pressure else "observed_quota_reset",
        "l4_fuel_minutes_to_deadline": fuel["minutes_to_deadline"],
        "l4_fuel_deadline_local": fuel["deadline_local"],
        "l4_fuel_next_deadline_local": fuel["next_deadline_local"],
        "l4_fuel_deadline_elapsed": fuel["deadline_elapsed"],
        "l4_fuel_local_now": fuel["local_now"],
        "l4_fuel_sprint_window_minutes": fuel["sprint_window_minutes"],
        "l4_fuel_queue_pressure": fuel["queue_pressure"],
        "l4_fuel_frontier_pressure": fuel["frontier_pressure"],
        "l4_fuel_time_pressure": fuel["time_pressure"],
        "l4_fuel_open_frontier": fuel["open_frontier"],
        "l4_fuel_recommended_target": fuel["recommended_target"],
        "l4_fuel_quota_observability": fuel["quota_observability"],
        "l4_fuel_quota_reset_at": fuel["quota_reset_at"],
        "l4_fuel_quota_minutes_to_reset": fuel["quota_minutes_to_reset"],
        "l4_fuel_reason": l4_reason,
        **codex_health,
    }


def main() -> int:
    args = parse_args()
    print(json.dumps(build_state(args), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
