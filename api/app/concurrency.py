"""自动并发（auto concurrency）决策：上下界、燃料状态、积压目标与最终决策。

搬移自 `orchestrator.Orchestrator`。判据：`scripts/audit_orchestrator_movable_mass.py --closure`
实测入口集 `_auto_concurrency_decision` / `_auto_concurrency_fuel_state` /
`_auto_concurrency_backlog_target` 闭包 **30 方法 / 506 行**，不触达任何不可搬方法。

**为什么用 ports（端口对象）而不是把状态拆成参数**：
这一簇里有 5 个方法读写 orchestrator 的实例状态（`_agent_semaphore`、
`_external_codex_process_cache`、`_agent_starts_by_root`、`_pending_codex_slot_reservations`）。
用户选定的混合方案要求「状态无关的走自由函数，状态耦合的留薄壳」，并且**零调用点改动**。
因此这里定义一个鸭子类型的 `ports`：它按 orchestrator 的**原名**暴露这一簇真正需要的读取入口，
orchestrator 直接把 `self` 传进来即可，不需要适配器。

结果是本模块的函数体与原文**逐行一致**，只有 `self.` → `ports.` 的机械替换：

| 原文 | 现在 |
|---|---|
| `self._get_task(db, task_id)` | `ports._get_task(db, task_id)` |
| `self._codex_slots_active()` | `ports._codex_slots_active()` |
| `self._agent_semaphore.capacity` | `ports._agent_semaphore.capacity` |
| `self.settings.X` | `ports.settings.X` |
| `self._auto_concurrency_*` | 本模块内的同级函数 |

这样状态所有权不变（仍由 orchestrator 持有），调用点不变，测试也没被削弱——
`tests/test_orchestrator.py` 里对 `_external_codex_processes` 的 9 处实例级 monkeypatch 继续生效。

依赖方向：本模块只依赖 `budget` / `models` 与标准库，**不得**导入 `orchestrator`。
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import budget
from .models import TaskNode, TaskStatus, utcnow
from .runtime_logs import ACTIVE_AGENT_STATUSES
from .utils import coerce_utc

if TYPE_CHECKING:
    from .config import Settings


ACTIVE_FRONTIER_STATUSES = {
    TaskStatus.PLANNED.value,
    TaskStatus.PLANNING.value,
    TaskStatus.DECOMPOSING.value,
    TaskStatus.QUEUED.value,
    TaskStatus.WAITING_CHILDREN.value,
    TaskStatus.RUNNING.value,
    TaskStatus.WORKING.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.ACCEPTING.value,
    TaskStatus.REPAIR.value,
}


class AutoConcurrencyPorts(Protocol):
    """orchestrator 侧需要提供的读取入口（按原名，便于直接传 `self`）。"""

    settings: Settings
    _agent_semaphore: Any
    _external_codex_process_cache: tuple | None
    _external_codex_process_scan_failed: bool
    _pending_codex_slot_reservations: dict

    def _get_task(self, db: Session, task_id: str) -> TaskNode: ...

    def _root_id_for_task(self, db: Session, task: TaskNode) -> str: ...

    def _subtree(self, tasks: list[TaskNode], root_id: str) -> list[TaskNode]: ...

    def _codex_slots_active(self, *, max_cache_age_seconds: float = 5.0) -> int: ...

    def _resource_pressure_state(self, db: Session, tasks: list[TaskNode]) -> tuple[bool, str]: ...

    def _codex_health_state(self) -> dict[str, Any]: ...

    def _machine_safety_pressure(self) -> tuple[bool, dict[str, Any]]: ...

    def _should_preserve_agent_budget(self, db: Session, root_id: str, upper: int) -> bool: ...


def auto_concurrency_backlog_target(
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


def auto_concurrency_fuel_state(
    settings: Settings,
    *,
    queued_work: int,
    active_frontier: int,
    active_agents: int,
    lower: int,
    upper: int,
    codex_health: dict[str, Any],
) -> dict[str, Any]:
    upper = max(1, int(upper))
    lower = max(1, min(int(lower), upper))
    deadline_hour = max(0, min(23, int(settings.auto_concurrency_fuel_deadline_hour)))
    offset_hours = max(-12.0, min(14.0, float(settings.auto_concurrency_fuel_timezone_offset_hours)))
    sprint_window = max(1, int(settings.auto_concurrency_fuel_sprint_window_minutes))
    local_tz = timezone(timedelta(hours=offset_hours))
    local_now = utcnow().astimezone(local_tz)
    deadline = local_now.replace(hour=deadline_hour, minute=0, second=0, microsecond=0)
    deadline_elapsed = local_now >= deadline
    next_deadline = deadline + timedelta(days=1) if deadline_elapsed else deadline
    minutes_to_deadline = 0 if deadline_elapsed else max(0, int((deadline - local_now).total_seconds() // 60))
    queue_threshold = max(1, int(settings.auto_concurrency_queue_threshold))
    queue_pressure = min(1.0, queued_work / max(1, queue_threshold * max(1, upper)))
    open_frontier = max(0, active_frontier - active_agents)
    frontier_pressure = min(1.0, open_frontier / max(1, upper))
    time_pressure = 0.0
    if minutes_to_deadline <= sprint_window:
        time_pressure = 1.0 - (minutes_to_deadline / sprint_window)
    score = round(max(0.0, min(1.0, queue_pressure * 0.45 + frontier_pressure * 0.35 + time_pressure * 0.20)), 3)
    quota_reset_at = coerce_utc(codex_health.get("codex_quota_reset_at"))
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


def auto_concurrency_decision(db: Session, task_id: str, ports: AutoConcurrencyPorts) -> dict[str, Any]:
    settings = ports.settings
    lower, upper = budget.auto_concurrency_bounds(settings)
    task = ports._get_task(db, task_id)
    root_id = ports._root_id_for_task(db, task)
    all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
    tasks = ports._subtree(all_tasks, root_id)
    counts = Counter(task.status for task in tasks)
    active_agent_statuses = sum(1 for item in tasks if item.status in ACTIVE_AGENT_STATUSES)
    codex_slots_active = ports._codex_slots_active()
    active_agents = max(active_agent_statuses, codex_slots_active)
    slot_pressure_agents = codex_slots_active
    queued_work = (
        counts[TaskStatus.PLANNED.value]
        + counts[TaskStatus.QUEUED.value]
        + counts[TaskStatus.REPAIR.value]
    )
    parent_ids = {item.parent_id for item in tasks if item.parent_id}
    active_frontier = sum(1 for item in tasks if item.id not in parent_ids and item.status in ACTIVE_FRONTIER_STATUSES)
    resource_pressure, pressure_reason = ports._resource_pressure_state(db, tasks)
    codex_health = ports._codex_health_state()
    if codex_health.get("codex_health_status") == "resource_block":
        resource_pressure = True
        pressure_reason = str(codex_health.get("codex_health_summary") or "Codex preflight reported resource backoff.")
    machine_pressure_for_fuel = False
    if settings.goal_mode:
        machine_pressure_for_fuel, machine_payload = ports._machine_safety_pressure()
        if machine_pressure_for_fuel:
            resource_pressure = True
            available = machine_payload.get("available_memory_mb")
            threshold = machine_payload.get("safe_min_available_memory_mb")
            pressure_reason = (
                f"Machine safety guard: available memory {available} MB is below the configured threshold "
                f"{threshold} MB."
            )
    queue_threshold = max(1, int(settings.auto_concurrency_queue_threshold))
    current_capacity = max(1, int(ports._agent_semaphore.capacity))
    fuel = auto_concurrency_fuel_state(
        settings,
        queued_work=queued_work,
        active_frontier=active_frontier,
        active_agents=slot_pressure_agents,
        lower=lower,
        upper=upper,
        codex_health=codex_health,
    )
    desired = lower
    target = lower
    ramp_step = max(1, min(4, max(1, upper // 3)))
    mode = "low_power"
    reason = f"L4低速巡航：排队 {queued_work}，前线 {active_frontier}，未到提速条件。"

    if not settings.auto_concurrency_enabled:
        desired = target = max(1, settings.max_concurrency)
        mode = "manual"
        reason = f"自动油门关闭，使用固定并发 {target}。"
    elif counts[TaskStatus.HIBERNATING.value] > 0:
        desired = target = lower
        mode = "hibernate_low_power"
        reason = "任务树存在休眠节点，保持低功率等待恢复点。"
    elif resource_pressure:
        desired = target = lower
        mode = "machine_safety_cooldown" if machine_pressure_for_fuel else "resource_cooldown"
        reason = f"检测到资源/流量压力，进入低功率冷却：{pressure_reason}"
    elif ports._should_preserve_agent_budget(db, root_id, upper):
        desired = target = lower
        mode = "budget_preserve"
        reason = "本轮 agent 启动预算接近下限，保持低功率并等待刷新或人工补充。"
    else:
        desired = auto_concurrency_backlog_target(
            lower=lower,
            upper=upper,
            queue_threshold=queue_threshold,
            queued_work=queued_work,
            active_frontier=active_frontier,
            active_agents=slot_pressure_agents,
        )
        fuel_recommended_target = max(lower, int(fuel.get("recommended_target") or lower))
        if fuel_recommended_target > desired:
            desired = fuel_recommended_target
        if fuel["phase"] in {"utilization_sprint", "backlog_sprint"} and desired > lower:
            desired = upper
        if desired > current_capacity and fuel["phase"] not in {"utilization_sprint", "backlog_sprint"}:
            target = min(desired, current_capacity + ramp_step)
        else:
            target = desired
        if target >= upper and desired >= upper:
            mode = "utilization_sprint" if fuel["phase"] == "utilization_sprint" else "high_throughput"
        elif target > current_capacity:
            mode = "ramp_up"
        elif target < current_capacity:
            mode = "ramp_down"
        elif target > lower:
            mode = "balanced"
        else:
            mode = "low_power"

        if mode == "utilization_sprint":
            if fuel.get("deadline_elapsed"):
                reason = (
                    f"L4加油点已过本地 {fuel['deadline_hour']}:00 消耗目标：未读到官方额度刷新点，"
                    f"前线 {active_frontier}、排队 {queued_work}，无压力，继续提到上限 {upper}，避免夜间额度闲置。"
                )
            else:
                reason = (
                    f"L4加油点进入冲刺：未读到官方额度刷新点，按本地 {fuel['deadline_hour']}:00 消耗目标还剩约 {fuel['minutes_to_deadline']} 分钟，"
                    f"前线 {active_frontier}、排队 {queued_work}，无压力，提到上限 {upper}。"
                )
        elif mode == "high_throughput":
            reason = f"队列和前线明显充足：前线 {active_frontier}，排队 {queued_work}，无压力，提到上限 {upper}。"
        elif mode == "ramp_up":
            reason = f"队列充足但未到冲刺档：从 {current_capacity} 分级爬坡到 {target}，避免一次性过冲。"
        elif mode == "balanced":
            reason = f"保持均衡档：排队 {queued_work}，前线 {active_frontier}，目标并发 {target}。"
        elif mode == "ramp_down":
            reason = f"可执行前线减少：从 {current_capacity} 降到 {target}。"

    target = max(lower, min(upper, int(target)))
    desired = max(lower, min(upper, int(desired)))
    return {
        "root_id": root_id,
        "enabled": int(settings.auto_concurrency_enabled),
        "mode": mode,
        "target": target,
        "desired": desired,
        "current_capacity": current_capacity,
        "ramp_step": ramp_step,
        "lower_bound": lower,
        "upper_bound": upper,
        "queue_threshold": queue_threshold,
        "queued_work": queued_work,
        "active_frontier": active_frontier,
        "active_agents": active_agents,
        "active_agent_statuses": active_agent_statuses,
        "codex_slots_active": codex_slots_active,
        "slot_pressure_agents": slot_pressure_agents,
        "resource_pressure": int(resource_pressure),
        "fuel_phase": fuel["phase"],
        "fuel_score": fuel["score"],
        "fuel_minutes_to_deadline": fuel["minutes_to_deadline"],
        "fuel_deadline_local": fuel["deadline_local"],
        "fuel_next_deadline_local": fuel["next_deadline_local"],
        "fuel_deadline_elapsed": fuel["deadline_elapsed"],
        "fuel_local_now": fuel["local_now"],
        "fuel_sprint_window_minutes": fuel["sprint_window_minutes"],
        "fuel_basis": fuel["basis"],
        "fuel_queue_pressure": fuel["queue_pressure"],
        "fuel_frontier_pressure": fuel["frontier_pressure"],
        "fuel_time_pressure": fuel["time_pressure"],
        "fuel_open_frontier": fuel["open_frontier"],
        "fuel_recommended_target": fuel["recommended_target"],
        "quota_observability": fuel["quota_observability"],
        "quota_reset_at": fuel["quota_reset_at"],
        "quota_minutes_to_reset": fuel["quota_minutes_to_reset"],
        "reason": reason,
    }
