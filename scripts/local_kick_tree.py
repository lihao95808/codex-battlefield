from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from state_paths import workspace_dir  # noqa: E402  (同一目录下的解析器)

# 每工程状态不在仓库里了：绑定工程后是 <状态根>/projects/<key>/。
WORKSPACE = workspace_dir()
RECENT_START_TTL_SECONDS = 120.0
RECOVERY_HOOK_INTERVAL_SECONDS = 30.0


def _read_current_tool_head() -> str:
    env_head = os.environ.get("CODEX_BATTLEFIELD_LOCAL_KICK_TOOL_HEAD", "").strip()
    if env_head:
        return env_head
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


LOCAL_KICK_START_TOOL_HEAD = _read_current_tool_head()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Codex Battlefield tree locally when the HTTP API is unresponsive.")
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--runner-mode", default="real")
    parser.add_argument("--max-concurrency", type=int, default=13)
    parser.add_argument("--fixed-concurrency", action="store_true")
    parser.add_argument("--resume-after-seconds", type=int, default=18_000)
    parser.add_argument("--machine-pressure-resume-after-seconds", type=int, default=1_800)
    parser.add_argument("--safe-min-available-memory-mb", type=int, default=4_096)
    parser.add_argument("--disable-secondary-lock", action="store_true")
    parser.add_argument("--allow-primary-with-secondary", action="store_true")
    parser.add_argument("--topoff-only", action="store_true")
    parser.add_argument("--no-controller-lock", action="store_true")
    return parser.parse_args()


def configure_environment(args: argparse.Namespace) -> None:
    os.environ["CODEX_BATTLEFIELD_RUNNER"] = args.runner_mode
    os.environ["CODEX_BATTLEFIELD_MAX_CONCURRENCY"] = str(max(1, args.max_concurrency))
    os.environ["CODEX_BATTLEFIELD_AUTO_CONCURRENCY_ENABLED"] = "0" if args.fixed_concurrency else "1"
    os.environ["CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MIN"] = "1"
    os.environ["CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MAX"] = str(max(1, args.max_concurrency))
    os.environ["CODEX_BATTLEFIELD_CODEX_TIMEOUT_SECONDS"] = "0"
    os.environ["CODEX_BATTLEFIELD_CODEX_IDLE_TIMEOUT_SECONDS"] = "1800"
    os.environ["CODEX_BATTLEFIELD_GODOT_TIMEOUT_SECONDS"] = "45"
    os.environ["CODEX_BATTLEFIELD_AUTO_HIBERNATE_ON_RESOURCE_BLOCK"] = "1"
    os.environ["CODEX_BATTLEFIELD_GOAL_MODE"] = "1"
    os.environ["CODEX_BATTLEFIELD_RESOURCE_BLOCK_RESUME_AFTER_SECONDS"] = str(max(60, args.resume_after_seconds))
    os.environ["CODEX_BATTLEFIELD_AGENT_BURST_LIMIT"] = "0"
    os.environ["CODEX_BATTLEFIELD_AGENT_BURST_RESERVE"] = "0"
    os.environ["CODEX_BATTLEFIELD_AGENT_BURST_RESUME_AFTER_SECONDS"] = str(max(60, args.resume_after_seconds))
    os.environ["CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB"] = str(max(0, args.safe_min_available_memory_mb))
    os.environ["CODEX_BATTLEFIELD_MACHINE_SAFETY_RESUME_AFTER_SECONDS"] = str(
        max(60, args.machine_pressure_resume_after_seconds)
    )
    os.environ["CODEX_BATTLEFIELD_OPEN_PREVIEW_ON_START"] = "0"
    os.environ["CODEX_BATTLEFIELD_PREVIEW_URL"] = "http://127.0.0.1:5173/"
    os.environ.pop("CODEX_BATTLEFIELD_CODEX_MODEL", None)
    os.environ.pop("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT", None)


def acquire_root_lock(root_id: str, suffix: str = "") -> TextIO | None:
    lock_dir = WORKSPACE / "local_kick_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (lock_dir / f"{root_id}{suffix}.lock").open("a+", encoding="utf-8")
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _close_lock_file_quietly(lock_file)
        return None
    try:
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(json.dumps({"pid": os.getpid(), "root_id": root_id, "suffix": suffix}, ensure_ascii=False))
        lock_file.flush()
    except OSError:
        _unlock_lock_file_quietly(lock_file)
        _close_lock_file_quietly(lock_file)
        return None
    return lock_file


def _unlock_lock_file_quietly(lock_file: TextIO) -> None:
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _close_lock_file_quietly(lock_file: TextIO) -> None:
    try:
        lock_file.close()
    except OSError:
        pass


def release_root_lock(lock_file: TextIO) -> None:
    try:
        _unlock_lock_file_quietly(lock_file)
    finally:
        _close_lock_file_quietly(lock_file)


def acquire_controller_lock(
    root_id: str,
    *,
    allow_secondary: bool = True,
    allow_primary_with_secondary: bool = False,
) -> tuple[TextIO | None, str, str]:
    primary = acquire_root_lock(root_id)
    if primary is not None:
        secondary_probe = acquire_root_lock(root_id, ".secondary")
        if secondary_probe is None:
            if allow_primary_with_secondary:
                return primary, "", "secondary_active_takeover"
            release_root_lock(primary)
            return None, "", "secondary_active"
        release_root_lock(secondary_probe)
        return primary, "", ""
    if not allow_secondary:
        return None, "", "primary_active"
    secondary = acquire_root_lock(root_id, ".secondary")
    if secondary is None:
        return None, "", "busy"
    return secondary, ".secondary", ""


async def run(args: argparse.Namespace) -> dict[str, object]:
    sys.path.insert(0, str(ROOT / "api"))
    from app.config import get_settings
    from app.database import init_db
    from app.orchestrator import Orchestrator, make_runner

    init_db()
    settings = get_settings()
    orchestrator = Orchestrator(settings)
    runner = make_runner(settings, args.runner_mode)
    active_tasks: dict[str, asyncio.Task[None]] = {}
    recent_started_at: dict[str, float] = {}
    total_started = 0
    root_kicked = False
    last_recovery_hook_at = 0.0

    while True:
        active_tasks = {task_id: task for task_id, task in active_tasks.items() if not task.done()}
        _prune_recent_starts(recent_started_at, set(active_tasks))
        await orchestrator._adjust_agent_concurrency(args.root_id, orchestrator._agent_semaphore)
        now = time.monotonic()
        if now - last_recovery_hook_at >= RECOVERY_HOOK_INTERVAL_SECONDS:
            _run_local_recovery_hooks(orchestrator, args.root_id)
            last_recovery_hook_at = now
        active_slots = _effective_codex_slots(orchestrator, args.root_id)
        capacity = max(1, orchestrator._agent_semaphore.capacity)
        if _local_kick_drain_requested(args.root_id):
            if active_tasks:
                await asyncio.wait(set(active_tasks.values()), timeout=10.0, return_when=asyncio.FIRST_COMPLETED)
                continue
            return {
                "status": "local_kick_drained",
                "root_id": args.root_id,
                "started": total_started,
                "active_slots_at_finish": active_slots,
                "capacity": capacity,
            }
        if _codex_resource_block_active(orchestrator):
            if active_tasks:
                await asyncio.wait(set(active_tasks.values()), timeout=10.0, return_when=asyncio.FIRST_COMPLETED)
                continue
            await orchestrator._auto_hibernate_retryable_resource_blocks(args.root_id)
            return {
                "status": "quota_cooldown",
                "root_id": args.root_id,
                "started": total_started,
                "active_slots_at_finish": active_slots,
                "capacity": capacity,
            }
        desired = max(0, min(capacity - active_slots, capacity))
        if desired > 0:
            reserved_slots = 0
            candidate_ids = _ready_leaf_candidates(
                orchestrator,
                args.root_id,
                desired,
                exclude_ids=set(active_tasks) | set(recent_started_at),
            )
            if candidate_ids:
                for task_id in candidate_ids:
                    latest_slots = _effective_codex_slots(orchestrator, args.root_id)
                    if not _has_launch_capacity(latest_slots, capacity):
                        break
                    if not orchestrator._try_reserve_pending_codex_slot_with_capacity(task_id, "local_topoff"):
                        break
                    active_tasks[task_id] = asyncio.create_task(
                        orchestrator._process_node(task_id, runner, orchestrator._agent_semaphore)
                    )
                    reserved_slots += 1
                    recent_started_at[task_id] = time.monotonic()
                    total_started += 1
                    _emit(
                        {
                            "event": "frontier_task_started",
                            "root_id": args.root_id,
                            "task_id": task_id,
                            "slots_before": latest_slots,
                            "slots_reserved": reserved_slots,
                            "capacity": capacity,
                            "active_supervised": len(active_tasks),
                        }
                    )
        if active_tasks:
            await asyncio.wait(set(active_tasks.values()), timeout=10.0, return_when=asyncio.FIRST_COMPLETED)
            continue
        pending_background_tasks = _pending_background_tasks(orchestrator)
        if pending_background_tasks:
            await asyncio.wait(pending_background_tasks, timeout=10.0, return_when=asyncio.FIRST_COMPLETED)
            continue
        active_slots = _effective_codex_slots(orchestrator, args.root_id)
        capacity = max(1, orchestrator._agent_semaphore.capacity)
        if active_slots >= capacity:
            await orchestrator._auto_hibernate_retryable_resource_blocks(args.root_id)
            return {
                "status": "frontier_top_off_completed",
                "root_id": args.root_id,
                "started": total_started,
                "active_slots_at_finish": active_slots,
                "capacity": capacity,
            }
        if root_kicked or args.topoff_only:
            await orchestrator._auto_hibernate_retryable_resource_blocks(args.root_id)
            return {
                "status": "frontier_top_off_no_ready_candidates",
                "root_id": args.root_id,
                "started": total_started,
                "active_slots_at_finish": active_slots,
                "capacity": capacity,
                "topoff_only": bool(args.topoff_only),
            }
        root_kicked = True
        _emit(
            {
                "event": "root_kick_fallback",
                "root_id": args.root_id,
                "slots": active_slots,
                "capacity": capacity,
            }
        )
        await orchestrator.run_tree(args.root_id, args.runner_mode)


def _run_local_recovery_hooks(orchestrator, root_id: str) -> dict[str, object]:
    results: dict[str, object] = {}
    hook_names = [
        "settle_unreachable_descendants_under_terminal_ancestors",
        "backfill_skill_note_children",
        "recover_deferred_parent_resumes",
        "recover_finished_agent_messages",
        "recover_finished_acceptance_messages",
        "cleanup_stalled_external_codex_agents",
        "_auto_retry_fast_retryable_blocks",
    ]
    for name in hook_names:
        hook = getattr(orchestrator, name, None)
        if hook is None:
            continue
        try:
            results[name] = hook(root_id)
        except Exception as exc:
            results[name] = {"status": "failed", "error": str(exc)}
    interesting = {
        name: value
        for name, value in results.items()
        if _recovery_hook_result_is_interesting(value)
    }
    if interesting:
        _emit({"event": "local_recovery_hooks", "root_id": root_id, "results": interesting})
    return results


def _recovery_hook_result_is_interesting(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("count", "recovered", "requeued", "adjusted", "settled_unreachable"):
        raw = value.get(key)
        if isinstance(raw, int) and raw > 0:
            return True
        if isinstance(raw, list) and raw:
            return True
    if value.get("status") == "failed":
        return True
    return False


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _has_launch_capacity(latest_slots: int, capacity: int) -> bool:
    return max(0, latest_slots) < max(1, capacity)


def _prune_recent_starts(
    recent_started_at: dict[str, float],
    active_task_ids: set[str],
    *,
    now: float | None = None,
    ttl_seconds: float = RECENT_START_TTL_SECONDS,
) -> None:
    now = time.monotonic() if now is None else now
    expired_ids = [
        task_id
        for task_id, started_at in recent_started_at.items()
        if task_id not in active_task_ids and now - started_at >= ttl_seconds
    ]
    for task_id in expired_ids:
        recent_started_at.pop(task_id, None)


def _pending_background_tasks(orchestrator) -> set[asyncio.Task]:
    tasks = getattr(orchestrator, "_background_tasks", set())
    if not tasks:
        return set()
    return {task for task in tasks if isinstance(task, asyncio.Task) and not task.done()}


def _effective_slot_count(process_slots: int, active_statuses: int) -> int:
    _ = active_statuses
    return max(0, int(process_slots))


def _effective_codex_slots(orchestrator, root_id: str) -> int:
    return _effective_slot_count(
        orchestrator._codex_slots_active(max_cache_age_seconds=2.0),
        _active_agent_status_count(orchestrator, root_id),
    )


def _codex_resource_block_active(orchestrator) -> bool:
    try:
        health = orchestrator._codex_health_state()
    except Exception:
        return False
    if health.get("codex_health_status") != "resource_block":
        return False
    reset_at_raw = health.get("codex_quota_reset_at")
    if not reset_at_raw:
        return True
    try:
        reset_at = datetime.fromisoformat(str(reset_at_raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    return reset_at.astimezone(timezone.utc) > datetime.now(timezone.utc)


def _local_kick_drain_requested(root_id: str) -> bool:
    drain_path = WORKSPACE / "local_kick_drain.json"
    if not drain_path.exists():
        return False
    try:
        payload = json.loads(drain_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    roots = payload.get("root_ids") or payload.get("roots")
    if isinstance(roots, list) and roots:
        if root_id not in {str(item) for item in roots}:
            return False
        return _drain_payload_applies_to_this_process(payload)
    root = payload.get("root_id")
    if root:
        if str(root) != root_id:
            return False
        return _drain_payload_applies_to_this_process(payload)
    if not bool(payload.get("drain", True)):
        return False
    return _drain_payload_applies_to_this_process(payload)


def _drain_payload_applies_to_this_process(payload: dict[str, object]) -> bool:
    replacement_head = str(
        payload.get("replacement_tool_head")
        or payload.get("current_tool_head")
        or payload.get("retire_before_tool_head")
        or ""
    ).strip()
    if replacement_head and LOCAL_KICK_START_TOOL_HEAD:
        if _tool_head_matches_or_descends_from(replacement_head, LOCAL_KICK_START_TOOL_HEAD):
            return False
    return bool(payload.get("drain", True))


def _tool_head_matches_or_descends_from(replacement_head: str, start_head: str) -> bool:
    replacement_ref = _normalize_git_ref(replacement_head)
    start_ref = _normalize_git_ref(start_head)
    if not replacement_ref or not start_ref:
        return False
    if replacement_ref == start_ref:
        return True
    if len(replacement_ref) >= 7 and start_ref.startswith(replacement_ref):
        return True
    if len(start_ref) >= 7 and replacement_ref.startswith(start_ref):
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", replacement_ref, start_ref],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _normalize_git_ref(value: str) -> str:
    return value.strip().lower()


def _active_agent_status_count(orchestrator, root_id: str) -> int:
    from app.database import SessionLocal
    from app.models import TaskNode
    from app.orchestrator import ACTIVE_AGENT_STATUSES
    from sqlalchemy import select

    with SessionLocal() as db:
        rows = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        subtree = orchestrator._subtree(rows, root_id)
        return sum(1 for task in subtree if task.status in ACTIVE_AGENT_STATUSES)


def _ready_leaf_candidates(orchestrator, root_id: str, limit: int, *, exclude_ids: set[str] | None = None) -> list[str]:
    from app.database import SessionLocal
    from app.models import TERMINAL_STATUSES, TaskNode, TaskStatus
    from app.orchestrator import ACTIVE_AGENT_STATUSES, STOPPED_STATUSES
    from sqlalchemy import select

    excluded = exclude_ids or set()
    with SessionLocal() as db:
        rows = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        subtree = orchestrator._subtree(rows, root_id)
        by_id = {task.id: task for task in subtree}
        children_by_parent: dict[str | None, list[TaskNode]] = {}
        for task in subtree:
            children_by_parent.setdefault(task.parent_id, []).append(task)
        parent_ids = {task.parent_id for task in subtree if task.parent_id}
        external_task_ids = {
            str(process.get("task_id") or "")
            for process in orchestrator._external_codex_processes(max_cache_age_seconds=0)
            if process.get("task_id")
        }
        candidates: list[tuple[tuple[int, int, object, str], str]] = []
        for task in subtree:
            if task.id in excluded:
                continue
            if task.id in parent_ids:
                continue
            if task.id in external_task_ids:
                continue
            if task.status in ACTIVE_AGENT_STATUSES or task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                continue
            if task.status not in {TaskStatus.PLANNED.value, TaskStatus.QUEUED.value, TaskStatus.REPAIR.value}:
                continue
            if not _ancestors_can_run(task, by_id):
                continue
            siblings = children_by_parent.get(task.parent_id, [])
            siblings_by_id = {sibling.id: sibling for sibling in siblings}
            dependency_ids = orchestrator._dependency_ids(task, siblings)
            if any(dependency_id not in siblings_by_id for dependency_id in dependency_ids):
                continue
            dependencies = [siblings_by_id[dependency_id] for dependency_id in dependency_ids]
            if orchestrator._is_skill_note_task(task):
                if not all(dep.status in TERMINAL_STATUSES or dep.status in STOPPED_STATUSES for dep in dependencies):
                    continue
            elif not all(dep.status == TaskStatus.DONE.value for dep in dependencies):
                continue
            pending_worker = orchestrator._pending_worker_result(task) is not None
            candidates.append((orchestrator._scheduler_ready_priority(task, pending_worker), task.id))
        candidates.sort(key=lambda item: item[0])
        return [task_id for _, task_id in candidates[: max(0, limit)]]


def _ancestors_can_run(task, by_id: dict[str, object]) -> bool:
    from app.models import TERMINAL_STATUSES
    from app.orchestrator import STOPPED_STATUSES

    parent_id = task.parent_id
    while parent_id:
        parent = by_id.get(parent_id)
        if parent is None:
            return False
        if parent.status in TERMINAL_STATUSES or parent.status in STOPPED_STATUSES:
            return False
        parent_id = parent.parent_id
    return True


def main() -> int:
    args = parse_args()
    configure_environment(args)
    lock_file = None
    lock_suffix = ""
    busy_reason = ""
    if args.no_controller_lock and not args.topoff_only:
        print(json.dumps({"status": "invalid", "root_id": args.root_id, "reason": "no_controller_lock_requires_topoff_only"}, ensure_ascii=False))
        return 2
    if not args.no_controller_lock:
        lock_file, lock_suffix, busy_reason = acquire_controller_lock(
            args.root_id,
            allow_secondary=not args.disable_secondary_lock,
            allow_primary_with_secondary=args.allow_primary_with_secondary,
        )
    if lock_file is None:
        if not args.no_controller_lock:
            print(json.dumps({"status": "busy", "root_id": args.root_id, "reason": busy_reason or "busy"}, ensure_ascii=False))
            return 0
        _emit({"event": "lockless_topoff_started", "root_id": args.root_id})
    if busy_reason == "secondary_active_takeover":
        _emit({"event": "primary_topoff_lock_acquired_with_secondary_active", "root_id": args.root_id})
    if lock_suffix:
        _emit({"event": "secondary_topoff_lock_acquired", "root_id": args.root_id})
    try:
        result = asyncio.run(run(args))
        if lock_suffix:
            result["lock_suffix"] = lock_suffix
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        if lock_file is not None:
            release_root_lock(lock_file)


if __name__ == "__main__":
    raise SystemExit(main())
