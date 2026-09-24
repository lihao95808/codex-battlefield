from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChildTaskSpec(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    scope: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    coverage_ids: list[str] = Field(default_factory=list)
    verification_kind: str = "evidence"
    estimated_leaf: bool = False


class PlannerDecision(BaseModel):
    decision: Literal["execute", "decompose", "blocked", "repair"]
    reason: str
    children: list[ChildTaskSpec] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    leaf_certainty: Literal["300%", "not_sure"] = "not_sure"
    why_not_leaf: str = ""
    coverage_ids: list[str] = Field(default_factory=list)
    integration_plan: str = ""
    verification_plan: str = ""


class WorkerResult(BaseModel):
    status: Literal["succeeded", "failed", "blocked", "needs_decomposition"]
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    suggested_children: list[ChildTaskSpec] = Field(default_factory=list)


class VerifierResult(BaseModel):
    verdict: Literal["pass", "fail", "repair", "blocked", "visual_uncertain"]
    reason: str
    checks: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    suggested_children: list[ChildTaskSpec] = Field(default_factory=list)
    leaf_misclassified: bool = False
    constitution_verdict: Literal["within_bounds", "below_floor", "above_ceiling", "not_checked"] = "not_checked"
    failure_to_decomposition_reason: str = ""


class AcceptanceResult(BaseModel):
    verdict: Literal["pass", "repair", "blocked", "visual_uncertain"]
    reason: str
    checks: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    suggested_children: list[ChildTaskSpec] = Field(default_factory=list)
    constitution_verdict: Literal["within_bounds", "below_floor", "above_ceiling", "not_checked"] = "not_checked"


class ConstitutionDraftRequest(BaseModel):
    goal: str = Field(min_length=1)
    target_path: str | None = None
    runner_mode: Literal["mock", "real"] | None = None


class ConstitutionUpdateRequest(BaseModel):
    markdown: str = Field(min_length=1)
    data: dict[str, Any]


class ConstitutionAmendRequest(BaseModel):
    """在树运行中修订宪法。

    `direction` **必填**且由人声明：产出方不能自己判断放宽还是收紧（那等于自己放水）。
    """

    markdown: str = Field(min_length=1)
    data: dict[str, Any]
    direction: Literal["tighten", "loosen", "mixed", "neutral"]
    actor: str = Field(min_length=1)
    note: str = ""
    # 节点级豁免：`{节点 id: 豁免理由}`。**理由必填**——豁免是"不带这个节点走新宪法"，
    # 不写理由就成了静默放宽（AGENTS 硬约定 4）。豁免只对**本次目标版本**有效：
    # 以后再有收紧，这个节点照样进再验证队列（`constitution_versions` 的债键含版本）。
    revalidate_exemptions: dict[str, str] = Field(default_factory=dict)


class ConstitutionResponse(BaseModel):
    id: str
    root_id: str | None
    goal: str
    target_path: str | None
    status: str
    version: int
    supersedes_id: str | None
    amendment_direction: str | None
    effective_at: datetime | None
    content_hash: str
    markdown: str
    data: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    locked_at: datetime | None


class ProjectStartRequest(BaseModel):
    goal: str = Field(min_length=1)
    target_path: str | None = None
    runner_mode: Literal["mock", "real"] | None = None
    constitution_id: str | None = None
    auto_run: bool = True


class OneShotProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)


class ProjectStartResponse(BaseModel):
    root_id: str
    status: str


class HibernateRequest(BaseModel):
    resume_after_seconds: int = Field(default=18_000, ge=60, le=604_800)
    reason: str = "等待 Codex/token 池刷新后自动恢复"


class BudgetGuardRequest(BaseModel):
    root_id: str | None = None
    resume_after_seconds: int = Field(default=18_000, ge=60, le=604_800)
    reason: str = "余额不足，保存状态并低功耗等待账户额度刷新。"
    hibernate_tree: bool = True


class BudgetGuardResponse(BaseModel):
    status: str
    root_id: str | None
    active: bool
    resume_at: datetime | None
    reason: str
    hibernated_count: int = 0


class TaskResponse(BaseModel):
    id: str
    parent_id: str | None
    title: str
    description: str
    depth: int
    status: str
    scope: list[str]
    acceptance: list[str]
    node_meta: dict[str, Any]
    constitution_id: str | None
    constitution_hash: str | None
    constitution_version: int | None
    leaf_attempts: int
    verification_failures: int
    converted_from_leaf: bool
    progress_current: int
    progress_total: int
    result: dict[str, Any] | None
    error_message: str | None
    branch: str | None
    worktree_path: str | None
    is_active_frontier: bool
    created_at: datetime
    updated_at: datetime


class RunResponse(BaseModel):
    id: int
    task_id: str
    kind: str
    status: str
    command: str
    sandbox: str
    exit_code: int | None
    result: dict[str, Any] | None
    logs_path: str | None
    prompt_path: str | None
    last_message_path: str | None
    workdir: str | None
    constitution_hash: str | None
    constitution_version: int | None
    started_at: datetime | None
    finished_at: datetime | None


class RunArtifactResponse(BaseModel):
    run_id: int
    kind: Literal["prompt", "logs", "last_message"]
    path: str | None
    exists: bool
    truncated: bool
    content: str


class WorktreeLifecycleBoard(BaseModel):
    """R3 worktree 生命周期看板：有多少、多少已回收、以及**为什么还被留着**。

    `by_skip_reason` 是刻意的：只说"还剩 12 个"没法判断是正常（保留期内 / failed 现场）
    还是卡住了（证据找不到 —— 那本身就是要处理的问题）。
    """

    with_worktree: int
    recycled: int
    recyclable_now: int
    by_skip_reason: dict[str, int]


class ModelTierBoard(BaseModel):
    """按角色分层的模型档（`docs/spec-verification-independence.md` §4）。

    `effective` 是**已解析**的结果（空档已回退全局），`tiered` 只列真正配了独立档的角色，
    `warnings` 是可自动判定的可疑之处。刻意不做"模型强弱排序"：名字里没有强度信息，硬排就是假判据。
    """

    effective: dict[str, str] = Field(default_factory=dict)
    tiered: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class WatchdogStateResponse(BaseModel):
    root_id: str | None
    requested_root_id: str | None
    selection: str
    root_status: str | None
    active_agents: int
    active_agent_statuses: int = 0
    codex_slots_active: int = 0
    codex_slots_capacity: int = 1
    hibernating: int
    active_frontier: int
    resume_due: bool
    orphaned_active_tree: bool
    should_resume: bool
    resume_at: datetime | None
    last_activity_at: datetime | None
    last_activity_age_seconds: int
    reason: str
    goal_mode: bool
    budget_warning_level: str
    budget_warning_message: str
    active_resource_pressure: bool = False
    active_resource_pressure_reason: str = ""
    blocked_active: int = 0
    blocked_superseded: int = 0
    blocked_under_repair: int = 0
    blocked_repaired: int = 0
    blocked_inactive_branch: int = 0
    blocked_resource_wait: int = 0
    machine_safety_recovered: bool = False
    worktrees: WorktreeLifecycleBoard | None = None
    model_tiers: ModelTierBoard | None = None
    budget_guard_active: bool = False
    budget_guard_resume_at: datetime | None = None
    budget_guard_reason: str = ""
    agent_burst_used: int
    agent_burst_limit: int
    agent_burst_reserve: int
    agent_burst_work_limit: int
    agent_burst_remaining: int
    recommended_action: str
    l4_fuel_mode: str | None = None
    l4_fuel_target: int | None = None
    l4_fuel_desired: int | None = None
    l4_fuel_current_capacity: int | None = None
    l4_fuel_phase: str | None = None
    l4_fuel_score: float | None = None
    l4_fuel_basis: str | None = None
    l4_fuel_recommended_target: int | None = None
    l4_fuel_open_frontier: int | None = None
    l4_fuel_time_pressure: float | None = None
    l4_fuel_minutes_to_deadline: int | None = None
    l4_fuel_deadline_local: datetime | None = None
    l4_fuel_next_deadline_local: datetime | None = None
    l4_fuel_deadline_elapsed: int | None = None
    l4_fuel_local_now: datetime | None = None
    l4_fuel_sprint_window_minutes: int | None = None
    l4_fuel_queue_pressure: float | None = None
    l4_fuel_frontier_pressure: float | None = None
    l4_fuel_quota_observability: str | None = None
    l4_fuel_quota_reset_at: datetime | None = None
    l4_fuel_quota_minutes_to_reset: int | None = None
    l4_fuel_reason: str | None = None
    watchdog_log_path: str
    watchdog_run_command: str
    watchdog_install_command: str
    watchdog_interval_minutes: int
    startup_hook_path: str | None
    startup_hook_exists: bool
    startup_hook_mode: str
    scheduled_task_path: str | None
    scheduled_task_exists: bool
    logon_task_path: str | None
    logon_task_exists: bool
    scheduled_task_name: str
    logon_task_name: str
    token_reserve_summary: str
    codex_health_path: str
    codex_health_exists: bool
    codex_health_status: str
    codex_health_checked_at: datetime | None = None
    codex_health_summary: str
    codex_quota_observability: str
    codex_quota_retry_after_seconds: int | None = None
    codex_quota_reset_at: datetime | None = None
    codex_quota_retry_after_source: str = ""
    codex_quota_signal: str = ""
    codex_quota_source: str = ""
    guardian_level: str = "L4"
    guardian_status: str = "unknown"
    guardian_mode: str = "observe"
    guardian_risk_level: str = "watch"
    guardian_summary: str = ""
    guardian_recommended_action: str = "observe"
    guardian_state_path: str = ""
    guardian_updated_at: datetime | None = None
    guardian_checks: list[dict[str, Any]] = Field(default_factory=list)
    api_restart_flag_path: str
    api_restart_pending: bool
    api_restart_drain_flag_path: str
    api_restart_drain_armed: bool
    active_agent_diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class WatchdogActionResponse(BaseModel):
    status: Literal["succeeded", "failed"]
    root_id: str | None
    command: str
    exit_code: int | None
    stdout: str
    stderr: str


class ApiRestartDrainResponse(BaseModel):
    status: Literal["armed", "disarmed"]
    api_restart_pending: bool
    api_restart_flag_path: str
    api_restart_drain_armed: bool
    api_restart_drain_flag_path: str


class EventResponse(BaseModel):
    id: int
    task_id: str | None
    type: str
    message: str
    payload: dict[str, Any]
    created_at: datetime


class TaskDetailResponse(BaseModel):
    task: TaskResponse
    runs: list[RunResponse]
    events: list[EventResponse]
    # 本节点自己欠的债：人清债点名的就是这个 id（§8.2），不在详情里列出来就没法清。
    obligations: list[dict[str, Any]] = []


class ObligationBoard(BaseModel):
    """树级债务看板（`docs/spec-parent-child-obligations.md` §9）。

    `oldest_blocking_age_seconds` 为 `None` 表示**没有可判定的时间戳**，不是"0 秒"——
    看板必须能区分"刚记上"与"时间戳判不出来"，所以这里不用 0 冒充。
    """

    blocking_total: int
    advisory_total: int
    unsettled_total: int
    by_type: dict[str, int]
    by_depth: dict[str, int]
    oldest_blocking_age_seconds: float | None
    revalidate_queue_depth: int
    settled_by_human_count: int
    settled_total: int
    recurrence_total: int


class ObligationIndexBoard(BaseModel):
    """债务**派生索引**的读面（`docs/spec-parent-child-obligations.md` §13 Q3）。

    这张表是 `node_meta["obligations"]` 的**投影**，不是第二个真相来源。所以看板必须同时给出
    "快照是哪一刻"与"它跟真相还对不对得上"，否则读的人拿不到能站住的结论：

    - `built_at is None` → 索引**从没建过**，跨树聚合没有读数（不是"没有债"）；
    - `in_sync is False` → 表已跟 `node_meta` 漂了（`drift_counts` 说明漂在哪一类），
      这时 `total` 可能**比真相小**。

    两种情况下的 `total == 0` 都**不代表没有债**——界面必须把这句话说出来。
    """

    status: str
    total: int
    by_type: dict[str, int] = Field(default_factory=dict)
    blocking_by_type: dict[str, int] = Field(default_factory=dict)
    by_root: dict[str, int] = Field(default_factory=dict)
    built_at: datetime | None = None
    in_sync: bool
    drift_counts: dict[str, int] = Field(default_factory=dict)
    rebuild_command: str


class FailureEntry(BaseModel):
    """一条失败/阻塞原始证据（R6）。`category` 缺省写 `unknown`——**不编造**类别。"""

    node_id: str
    title: str
    depth: int | None = None
    status: str
    worker_status: str = ""
    category: str
    retryable: bool | None = None
    parent_action: str = ""
    source: str
    reason: str
    blocked_child_count: int = 0
    blocked_child_reports: list[dict[str, Any]] = Field(default_factory=list)


class FailureBoard(BaseModel):
    """树级失败面看板（`docs/spec-resource-governance.md` §R6）。

    按 `category` / `retryable` / `parent_action` 三条线聚合，并带有界的最浅若干条明细：
    看板要能一眼回答"这棵树在哪些类别上失败过"，而不是让人去翻每个节点的 `node_meta`。
    """

    total: int
    by_category: dict[str, int]
    by_parent_action: dict[str, int]
    by_retryable: dict[str, int]
    entries: list[FailureEntry]


class SelfAssessmentBoard(BaseModel):
    """自评校准读数（`docs/spec-verification-independence.md` §5）。

    **只读**：`overconfidence_ratio` 没有样本时是 `None`（不是 0），且不触发任何自动动作——
    阈值属政策输入，不由实现方定。
    """

    claims: int
    misses: int
    overconfidence_ratio: float | None = None
    by_depth: dict[str, dict[str, int]] = Field(default_factory=dict)
    policy: str = ""


class ObligationSettleRequest(BaseModel):
    """人工清债。`reason` 与 `actor` 都必填：放宽不是禁止的，**无声的**放宽才是（§8.2）。"""

    obligation_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ConstitutionBoard(BaseModel):
    """宪法传播看板（`docs/spec-constitution-amendment.md` §9）。

    回答"传播完成了吗"。`stale_by_action` 七个桶全给（含空桶）：少一个键，消费方就会把
    "没有"读成"未知"。`effective_version` 为 `None` 表示这棵树还没绑定宪法。
    """

    effective_version: int | None
    effective_hash: str | None
    node_total: int
    version_distribution: dict[str, int]
    stale_total: int
    stale_by_action: dict[str, list[str]]
    running_on_old_version: int
    revalidate_queue_depth: int
    last_amendment: dict[str, Any] | None


class TreeResponse(BaseModel):
    tasks: list[TaskResponse]
    edges: list[dict[str, str]]
    stats: dict[
        str,
        int
        | float
        | str
        | bool
        | None
        | ObligationBoard
        | ConstitutionBoard
        | FailureBoard
        | ModelTierBoard
        | SelfAssessmentBoard,
    ]
    events: list[EventResponse]
    constitution: ConstitutionResponse | None = None
    usage: dict[str, Any] | None = None
