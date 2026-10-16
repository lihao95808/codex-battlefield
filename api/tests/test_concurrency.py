from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import concurrency
from app.models import TaskNode
from app.orchestrator import Orchestrator


class _Settings:
    def __init__(self, **overrides: object) -> None:
        self.auto_concurrency_enabled = True
        self.auto_concurrency_min = 1
        self.auto_concurrency_max = 4
        self.max_concurrency = 2
        self.auto_concurrency_queue_threshold = 3
        self.auto_concurrency_fuel_deadline_hour = 6
        self.auto_concurrency_fuel_timezone_offset_hours = 8
        self.auto_concurrency_fuel_sprint_window_minutes = 45
        self.goal_mode = False
        self.machine_safety_min_available_memory_mb = 0
        for key, value in overrides.items():
            setattr(self, key, value)


def _task(task_id: str, *, parent_id: str | None = None, status: str = "queued") -> TaskNode:
    return TaskNode(
        id=task_id,
        parent_id=parent_id,
        title=task_id,
        status=status,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class _Semaphore:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity


class _Ports:
    """`auto_concurrency_decision` 的端口替身，按 orchestrator 的原名暴露读取入口。"""

    def __init__(self, settings: _Settings, **overrides: object) -> None:
        self.settings = settings
        self._agent_semaphore = _Semaphore(2)
        self._external_codex_process_cache = None
        self._external_codex_process_scan_failed = False
        self._pending_codex_slot_reservations = {}
        self.slots_active = 0
        self.pressure = (False, "")
        self.health: dict[str, object] = {}
        self.machine = (False, {})
        self.preserve = False
        for key, value in overrides.items():
            setattr(self, key, value)

    def _get_task(self, db: object, task_id: str) -> TaskNode:
        return next(task for task in db.all() if task.id == task_id)  # type: ignore[attr-defined]

    def _root_id_for_task(self, db: object, task: TaskNode) -> str:
        return "root-1"

    def _subtree(self, tasks: list[TaskNode], root_id: str) -> list[TaskNode]:
        return [task for task in tasks if task.id == root_id or task.parent_id == root_id]

    def _codex_slots_active(self, *, max_cache_age_seconds: float = 5.0) -> int:
        return self.slots_active

    def _resource_pressure_state(self, db: object, tasks: list[TaskNode]) -> tuple[bool, str]:
        return self.pressure

    def _codex_health_state(self) -> dict[str, object]:
        return self.health

    def _machine_safety_pressure(self) -> tuple[bool, dict[str, object]]:
        return self.machine

    def _should_preserve_agent_budget(self, db: object, root_id: str, upper: int) -> bool:
        return self.preserve


class _Db:
    """任务实际来自 db（决策函数只从 db 取树），因此替身必须真的返回它们。"""

    def __init__(self, tasks: list[TaskNode] | None = None) -> None:
        self._tasks = list(tasks or [])

    def scalars(self, statement: object) -> _Db:
        return self

    def all(self) -> list[TaskNode]:
        return list(self._tasks)


def test_backlog_target_stays_low_without_work() -> None:
    assert concurrency.auto_concurrency_backlog_target(
        lower=1, upper=4, queue_threshold=3, queued_work=0, active_frontier=5, active_agents=0
    ) == 1
    # 上限等于下限时没有可调空间
    assert concurrency.auto_concurrency_backlog_target(
        lower=2, upper=2, queue_threshold=3, queued_work=99, active_frontier=99, active_agents=0
    ) == 2
    # 前线被 agent 占满 → 不加并发
    assert concurrency.auto_concurrency_backlog_target(
        lower=1, upper=4, queue_threshold=3, queued_work=99, active_frontier=2, active_agents=2
    ) == 1


def test_backlog_target_climbs_with_queue_depth() -> None:
    common = dict(lower=1, upper=4, queue_threshold=3, active_frontier=8, active_agents=0)
    # 低于 max(2, queue_threshold // 2) 时保持下限
    assert concurrency.auto_concurrency_backlog_target(queued_work=1, **common) == 1
    assert concurrency.auto_concurrency_backlog_target(queued_work=2, **common) == 2
    # 排队刚到阈值且 active_frontier >= upper → 直接给上限
    assert concurrency.auto_concurrency_backlog_target(queued_work=3, **common) == 4
    assert concurrency.auto_concurrency_backlog_target(queued_work=6, **common) == 4
    assert concurrency.auto_concurrency_backlog_target(queued_work=9, **common) == 4

    # 前线不足上限时走到「可运行上限」分支：active_frontier=3, lower=1 → ceiling=3
    narrow = dict(lower=1, upper=4, queue_threshold=3, active_frontier=3, active_agents=0)
    assert concurrency.auto_concurrency_backlog_target(queued_work=2, **narrow) == 2
    assert concurrency.auto_concurrency_backlog_target(queued_work=3, **narrow) == 3
    assert concurrency.auto_concurrency_backlog_target(queued_work=9, **narrow) == 3


def test_fuel_state_coasts_without_work() -> None:
    state = concurrency.auto_concurrency_fuel_state(
        _Settings(), queued_work=0, active_frontier=0, active_agents=0, lower=1, upper=4, codex_health={}
    )
    assert state["phase"] == "coast"
    assert state["basis"] == "queue_pressure"
    assert state["recommended_target"] == 1
    assert state["quota_reset_at"] is None
    assert state["quota_minutes_to_reset"] is None
    # 没有排队/前线压力时，score 只可能来自「加油点时间压力」，因此 ∈ [0, 1]
    assert 0.0 <= state["score"] <= 1.0
    assert state["score"] == round(state["time_pressure"] * 0.20, 3)


def test_fuel_state_quota_wait_zeroes_score() -> None:
    reset_at = datetime.now(timezone.utc) + timedelta(hours=1)
    state = concurrency.auto_concurrency_fuel_state(
        _Settings(),
        queued_work=50,
        active_frontier=50,
        active_agents=0,
        lower=1,
        upper=4,
        # 注意：该字段必须是 datetime，不是 ISO 字符串（真实来源 _codex_health_state 已解析成 datetime）
        codex_health={"codex_health_status": "resource_block", "codex_quota_reset_at": reset_at},
    )
    assert state["phase"] == "quota_wait"
    assert state["basis"] == "observed_quota_reset"
    assert state["score"] == 0.0
    assert state["quota_minutes_to_reset"] is not None


def test_fuel_state_backlog_sprint_pushes_to_upper() -> None:
    state = concurrency.auto_concurrency_fuel_state(
        _Settings(), queued_work=1000, active_frontier=1000, active_agents=0, lower=1, upper=4, codex_health={}
    )
    assert state["phase"] == "backlog_sprint"
    assert state["recommended_target"] == 4
    assert state["queue_pressure"] == 1.0
    assert state["frontier_pressure"] == 1.0


def test_fuel_deadline_hour_wraps_at_midnight(monkeypatch) -> None:
    """加油点"已过"的判定在本地 0 点会翻面 —— 写判据的人必须知道这件事。

    背景：`test_orchestrator.py::test_auto_concurrency_sprints_after_fuel_deadline_when_queue_remains`
    为了在**任意时刻**都能落到"已过加油点"分支，用 `deadline_hour = now.hour - 1` 现算
    （旧写法是 `now.hour if now.minute > 0 else (now.hour - 1) % 24`）。问题出在
    `local_now(00:xx) >= deadline(23:00)` 为假：本地 0 点时"减一小时"绕回前一天 23 点，
    "已过"分支当天不可达。旧写法只在 `minute == 0` 时才走那条分支，所以那条判据的
    失败窗口是**每天的头一分钟**（00:00:00–00:00:59）——一分钟的红，比一小时的更难遇到、
    也更容易被当成偶发。

    这里把两种写法并排钉住：`deadline=23`（旧写法在本地 0 点的取值）落不到 elapsed 分支，
    `deadline=0` 能落到。判定全走真实生产函数，不复制公式。
    """
    monkeypatch.setattr(concurrency, "utcnow", lambda: datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc))
    common = dict(
        queued_work=10,
        active_frontier=10,
        active_agents=0,
        lower=1,
        upper=13,
        codex_health={},
    )

    # 旧写法在本地 0 点算出的 deadline（= 23 点）：还没到，"已过"分支不可达
    wrapped = concurrency.auto_concurrency_fuel_state(
        _Settings(auto_concurrency_fuel_deadline_hour=23, auto_concurrency_fuel_timezone_offset_hours=0),
        **common,
    )
    assert wrapped["phase"] != "utilization_sprint"
    assert wrapped["basis"] != "operator_utilization_deadline_elapsed"
    assert wrapped["time_pressure"] == 0.0

    # 本地 0 点该有的写法（deadline = 0 点）：已过，落到"已过"分支
    elapsed = concurrency.auto_concurrency_fuel_state(
        _Settings(auto_concurrency_fuel_deadline_hour=0, auto_concurrency_fuel_timezone_offset_hours=0),
        **common,
    )
    assert elapsed["phase"] == "utilization_sprint"
    assert elapsed["basis"] == "operator_utilization_deadline_elapsed"
    # 上限 13，但前线只有 10 个可跑，所以推荐值被 active_frontier 夹到 10
    assert elapsed["recommended_target"] == 10


def test_decision_manual_mode_overrides_everything() -> None:
    tasks = [_task("root-1", status="running")]
    ports = _Ports(_Settings(auto_concurrency_enabled=False, max_concurrency=7))
    decision = concurrency.auto_concurrency_decision(_Db(tasks), "root-1", ports)
    assert decision["mode"] == "manual"
    assert decision["target"] == 7
    assert decision["desired"] == 7
    assert decision["enabled"] == 0
    assert "自动油门关闭" in decision["reason"]


def test_decision_hibernating_forces_low_power() -> None:
    tasks = [_task("root-1", status="hibernating"), _task("c1", parent_id="root-1", status="queued")]
    ports = _Ports(_Settings())
    decision = concurrency.auto_concurrency_decision(_Db(tasks), "root-1", ports)
    assert decision["mode"] == "hibernate_low_power"
    assert decision["queued_work"] == 1


def test_decision_resource_pressure_modes() -> None:
    tasks = [_task("root-1", status="running")]
    ports = _Ports(_Settings(), pressure=(True, "限流中"))
    decision = concurrency.auto_concurrency_decision(_Db(tasks), "root-1", ports)
    assert decision["mode"] == "resource_cooldown"
    assert decision["resource_pressure"] == 1
    assert "限流中" in decision["reason"]

    # goal 模式下的机器安全压力走另一个模式名
    goal_ports = _Ports(
        _Settings(goal_mode=True),
        pressure=(False, ""),
        machine=(True, {"available_memory_mb": 128, "safe_min_available_memory_mb": 4096}),
    )
    goal_decision = concurrency.auto_concurrency_decision(_Db(tasks), "root-1", goal_ports)
    assert goal_decision["mode"] == "machine_safety_cooldown"
    assert "available memory 128 MB" in goal_decision["reason"]


def test_decision_health_resource_block_is_pressure() -> None:
    tasks = [_task("root-1", status="running")]
    ports = _Ports(_Settings(), health={"codex_health_status": "resource_block", "codex_health_summary": "backoff"})
    decision = concurrency.auto_concurrency_decision(_Db(tasks), "root-1", ports)
    assert decision["resource_pressure"] == 1
    assert "backoff" in decision["reason"]


def test_decision_budget_preserve() -> None:
    tasks = [_task("root-1", status="running")]
    ports = _Ports(_Settings(), preserve=True)
    decision = concurrency.auto_concurrency_decision(_Db(tasks), "root-1", ports)
    assert decision["mode"] == "budget_preserve"
    assert decision["target"] == decision["lower_bound"]


def test_decision_ramp_up_and_caps() -> None:
    # 8 个排队 + 1 个运行，上限 4、当前容量 2 → 只能爬一步
    tasks = [_task("root-1", status="running")] + [_task(f"c{i}", parent_id="root-1", status="queued") for i in range(8)]
    ports = _Ports(_Settings())
    decision = concurrency.auto_concurrency_decision(_Db(tasks), "root-1", ports)
    assert decision["mode"] in {"ramp_up", "balanced", "high_throughput", "utilization_sprint"}
    assert decision["lower_bound"] <= decision["target"] <= decision["upper_bound"]
    assert decision["lower_bound"] <= decision["desired"] <= decision["upper_bound"]
    assert decision["current_capacity"] == 2
    assert decision["ramp_step"] >= 1
    assert decision["queued_work"] == 8
    # 子节点是树叶，全部计入前线；root-1 因为有子节点而被排除在前线之外
    assert decision["active_frontier"] == 8
    assert decision["codex_slots_active"] == 0
    assert decision["slot_pressure_agents"] == 0


def test_decision_counts_external_slots_as_agents() -> None:
    tasks = [_task("root-1", status="running")]
    ports = _Ports(_Settings(), slots_active=3)
    decision = concurrency.auto_concurrency_decision(_Db(tasks), "root-1", ports)
    assert decision["codex_slots_active"] == 3
    assert decision["active_agents"] == 3
    assert decision["active_agent_statuses"] == 1


def test_orchestrator_delegates_agree_with_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """反漂移：orchestrator 的委托必须与模块函数给出同一答案。

    ⚠️ 必须**冻住时钟**。`auto_concurrency_fuel_state` 里有 `minutes_to_deadline`
    与 `deadline_elapsed` 两个**分钟级**的时钟量；两次调用之间正好跨过一分钟边界时，
    两边给出的字典会不一样 —— 实测在整套跑时偶发红（单跑必绿，典型的假失败）。
    这不是"忽略一下就好了"的 flake：它会让真正的委托漂移淹在噪声里。

    冻时钟同时也让这条判据**更严**：两边看到的是同一时刻，差异只可能来自实现。
    """
    frozen = datetime(2026, 10, 16, 19, 30, tzinfo=timezone.utc)  # 本地 03:30，未过 20:00 截止
    monkeypatch.setattr(concurrency, "utcnow", lambda: frozen)

    orchestrator = Orchestrator.__new__(Orchestrator)

    backlog_args = dict(lower=1, upper=4, queue_threshold=3, queued_work=6, active_frontier=8, active_agents=0)
    assert orchestrator._auto_concurrency_backlog_target(**backlog_args) == (
        concurrency.auto_concurrency_backlog_target(**backlog_args)
    )

    orchestrator.settings = _Settings()
    fuel_args = dict(queued_work=6, active_frontier=8, active_agents=0, lower=1, upper=4, codex_health={})
    assert orchestrator._auto_concurrency_fuel_state(**fuel_args) == (
        concurrency.auto_concurrency_fuel_state(orchestrator.settings, **fuel_args)
    )

    # ports 直接传 self：确认签名确实接受 orchestrator 本身
    ports = _Ports(_Settings())
    assert concurrency.auto_concurrency_decision(_Db([_task("root-1", status="running")]), "root-1", ports)["root_id"] == "root-1"
    assert orchestrator._auto_concurrency_bounds() == (1, 4)
