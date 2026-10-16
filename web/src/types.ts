export type TaskStatus =
  | 'planned'
  | 'planning'
  | 'decomposing'
  | 'queued'
  | 'waiting_children'
  | 'running'
  | 'working'
  | 'verifying'
  | 'accepting'
  | 'done'
  | 'repair'
  | 'blocked'
  | 'failed'
  | 'paused'
  | 'hibernating'
  | 'canceled';

export interface BattlefieldTask {
  id: string;
  parent_id: string | null;
  title: string;
  description: string;
  depth: number;
  status: TaskStatus;
  scope: string[];
  acceptance: string[];
  node_meta: Record<string, unknown>;
  constitution_id: string | null;
  constitution_hash: string | null;
  constitution_version: number | null;
  leaf_attempts: number;
  verification_failures: number;
  converted_from_leaf: boolean;
  progress_current: number;
  progress_total: number;
  result: Record<string, unknown> | null;
  error_message: string | null;
  branch: string | null;
  worktree_path: string | null;
  is_active_frontier: boolean;
  created_at: string;
  updated_at: string;
}

export interface BattlefieldRun {
  id: number;
  task_id: string;
  kind: string;
  status: string;
  command: string;
  sandbox: string;
  exit_code: number | null;
  result: Record<string, unknown> | null;
  logs_path: string | null;
  prompt_path: string | null;
  last_message_path: string | null;
  workdir: string | null;
  constitution_hash: string | null;
  constitution_version: number | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunArtifactResponse {
  run_id: number;
  kind: 'prompt' | 'logs' | 'last_message';
  path: string | null;
  exists: boolean;
  truncated: boolean;
  content: string;
}

export interface ActiveAgentDiagnostic {
  task_id: string;
  title: string;
  status: TaskStatus | string;
  depth: number;
  phase: string;
  severity: 'running' | 'warning' | 'stalled' | string;
  summary: string;
  last_task_update_at: string | null;
  latest_run_id: number | null;
  latest_run_kind: string | null;
  latest_run_status: string | null;
  latest_progress_path: string | null;
  latest_progress_at: string | null;
  prompt_path: string | null;
  prompt_at: string | null;
  no_progress_seconds: number;
  idle_timeout_seconds: number;
  markers: string[];
  tail_summary: string;
}

export interface GuardianCheck {
  key: string;
  label: string;
  status: 'ok' | 'warning' | 'critical' | 'sleeping' | 'idle' | string;
  detail: string;
  action: string;
}

export interface WatchdogStateResponse {
  root_id: string | null;
  requested_root_id: string | null;
  selection: string;
  root_status: TaskStatus | null;
  active_agents: number;
  active_agent_statuses: number;
  codex_slots_active: number;
  codex_slots_capacity: number;
  hibernating: number;
  active_frontier: number;
  resume_due: boolean;
  orphaned_active_tree: boolean;
  should_resume: boolean;
  resume_at: string | null;
  last_activity_at: string | null;
  last_activity_age_seconds: number;
  reason: string;
  goal_mode: boolean;
  budget_warning_level: string;
  budget_warning_message: string;
  active_resource_pressure: boolean;
  active_resource_pressure_reason: string;
  blocked_active: number;
  blocked_superseded: number;
  blocked_under_repair: number;
  blocked_repaired: number;
  blocked_inactive_branch: number;
  blocked_resource_wait: number;
  budget_guard_active: boolean;
  budget_guard_resume_at: string | null;
  budget_guard_reason: string;
  agent_burst_used: number;
  agent_burst_limit: number;
  agent_burst_reserve: number;
  agent_burst_work_limit: number;
  agent_burst_remaining: number;
  recommended_action: string;
  l4_fuel_mode?: string | null;
  l4_fuel_target?: number | null;
  l4_fuel_desired?: number | null;
  l4_fuel_current_capacity?: number | null;
  l4_fuel_phase?: string | null;
  l4_fuel_score?: number | null;
  l4_fuel_basis?: string | null;
  l4_fuel_recommended_target?: number | null;
  l4_fuel_open_frontier?: number | null;
  l4_fuel_time_pressure?: number | null;
  l4_fuel_minutes_to_deadline?: number | null;
  l4_fuel_deadline_local?: string | null;
  l4_fuel_next_deadline_local?: string | null;
  l4_fuel_deadline_elapsed?: number | null;
  l4_fuel_local_now?: string | null;
  l4_fuel_sprint_window_minutes?: number | null;
  l4_fuel_queue_pressure?: number | null;
  l4_fuel_frontier_pressure?: number | null;
  l4_fuel_quota_observability?: string | null;
  l4_fuel_quota_reset_at?: string | null;
  l4_fuel_quota_minutes_to_reset?: number | null;
  l4_fuel_reason?: string | null;
  watchdog_log_path: string;
  watchdog_run_command: string;
  watchdog_install_command: string;
  watchdog_interval_minutes: number;
  startup_hook_path: string | null;
  startup_hook_exists: boolean;
  startup_hook_mode: string;
  scheduled_task_path: string | null;
  scheduled_task_exists: boolean;
  logon_task_path: string | null;
  logon_task_exists: boolean;
  scheduled_task_name: string;
  logon_task_name: string;
  token_reserve_summary: string;
  codex_health_path: string;
  codex_health_exists: boolean;
  codex_health_status: string;
  codex_health_checked_at: string | null;
  codex_health_summary: string;
  codex_quota_observability: string;
  codex_quota_retry_after_seconds?: number | null;
  codex_quota_reset_at?: string | null;
  codex_quota_retry_after_source?: string;
  codex_quota_signal?: string;
  codex_quota_source?: string;
  guardian_level?: string;
  guardian_status?: string;
  guardian_mode?: string;
  guardian_risk_level?: string;
  guardian_summary?: string;
  guardian_recommended_action?: string;
  guardian_state_path?: string;
  guardian_updated_at?: string | null;
  guardian_checks?: GuardianCheck[];
  api_restart_flag_path: string;
  api_restart_pending: boolean;
  api_restart_drain_flag_path: string;
  api_restart_drain_armed: boolean;
  active_agent_diagnostics: ActiveAgentDiagnostic[];
}

export interface WatchdogActionResponse {
  status: 'succeeded' | 'failed';
  root_id: string | null;
  command: string;
  exit_code: number | null;
  stdout: string;
  stderr: string;
}

export interface ApiRestartDrainResponse {
  status: 'armed' | 'disarmed';
  api_restart_pending: boolean;
  api_restart_flag_path: string;
  api_restart_drain_armed: boolean;
  api_restart_drain_flag_path: string;
}

export interface BudgetGuardResponse {
  status: string;
  root_id: string | null;
  active: boolean;
  resume_at: string | null;
  reason: string;
  hibernated_count: number;
}

export interface BattlefieldEvent {
  id: number;
  task_id: string | null;
  type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}

/**
 * `/api/state` —— 状态布局自描述。
 *
 * 为什么界面要显示它：状态与工程解耦之后，最危险的错误形态是
 * **"看着是这个工程、其实在改另一个"** —— 路径都对、进程也不报错，
 * 只有库里的内容不对。把"进程实际在用的工程/库/状态根"摆在界面上，
 * 这种错位就变成一眼可见，而不是等到发现产物跑到别处才察觉。
 */
/**
 * 管家（steward）的对话类型。`docs/spec-steward.md`。
 *
 * `read_only_mode` 出现在每个回复里：界面必须能把"它只是建议"说清楚，
 * 否则用户会以为管家已经替他做了。
 */
export interface StewardProposedAction {
  action: string;
  target: string;
  why: string;
  risk: string;
}

export interface StewardMessageRecord {
  id: number;
  role: 'user' | 'steward' | 'system' | string;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

export interface StewardMessagesResponse {
  read_only_mode: boolean;
  messages: StewardMessageRecord[];
}

export interface StewardChatResponse {
  reply: string;
  situation: string;
  proposed_actions: StewardProposedAction[];
  /** 已落成待批准动作的 id（与 `proposed_actions` 顺序对应，不合法的那些不在里面）。 */
  proposed_action_ids?: number[];
  /** 被服务端拒掉的建议及原因（模型幻觉出的动作名、缺参数等）。 */
  rejected_actions?: string[];
  needs_user_input: boolean;
  confidence: 'high' | 'medium' | 'low';
  exit_code?: number;
  read_only_mode: boolean;
  error?: boolean;
}

/** 管家提议的**动作**（不是任务）。状态机见 docs/spec-steward.md。 */
export interface StewardActionRecord {
  id: number;
  action: string;
  target: string;
  why: string;
  risk: string;
  status: 'pending' | 'approved' | 'rejected' | 'executed' | 'failed' | string;
  result: string;
  error: string;
  created_at: string | null;
  decided_at: string | null;
  executed_at: string | null;
}

export interface StewardActionsResponse {
  read_only_mode: boolean;
  pending_count: number;
  actions: StewardActionRecord[];
}

export interface StewardActionDecision {
  ok: boolean;
  message?: string;
  action: StewardActionRecord | null;
}

export interface StewardAlert {
  level: 'info' | 'warning' | 'action' | string;
  kind: string;
  message: string;
}

export interface StewardAlertsResponse {
  alerts: StewardAlert[];
  counts: Record<string, number>;
}

/**
 * 一次**推送**（SSE `alerts` 事件）。比拉取多三个字段，都是"为什么现在说话"。
 *
 * `worth_pushing` 与 `alerts` 分开是有意的：`alerts` 是**全量现状**（界面要显示
 * 全部），`worth_pushing` 是**够格打扰你的那些**。界面据此决定高亮还是安静地列着。
 */
export interface StewardAlertPush {
  alerts: StewardAlert[];
  worth_pushing: StewardAlert[];
  counts: Record<string, number>;
  /** 为什么现在推。用户最想知道的是"这跟上次有何不同"。 */
  reason: string;
  /** 当前的打扰阈值，让界面能解释"为什么这条没弹"。 */
  min_level: string;
}

export interface StateLayoutResponse {
  state_home: string;
  project_key: string;
  project_root: string | null;
  bound: boolean;
  tools_dir: string;
  project_dir?: string;
  db_path?: string;
  runs_dir?: string;
  worktrees_dir?: string;
  database_url: string;
  runner_mode: string;
  /** 磁盘上 `active.json` 现在写的工程（进程**没有**回读它）。 */
  active_file_key: string;
  active_file_root: string | null;
  /**
   * 进程的启动快照与磁盘现状是否一致。
   * false = 用户切过工程但**没重启**，当前进程仍用旧的那个（决策 D1：不热切库）。
   */
  snapshot_matches_active_file: boolean;
}

/** 一个已挂载的工程（`GET /api/projects`）。 */
export interface MountedProject {
  key: string;
  root: string;
  project_dir: string;
  db_path: string;
  db_exists: boolean;
  db_size_bytes: number;
  /** 工程目录现在是否真的存在（可能被删或移走过）。 */
  root_exists: boolean;
  /** 是否为当前工程。 */
  active: boolean;
  /**
   * 状态目录与工程是否同一块盘。false 时 `git worktree` 不可靠 ——
   * 这是"能绑但会出错"的情形，界面必须提前说，而不是等跑到一半才炸。
   */
  drive_matches: boolean;
}

export interface MountedProjectsResponse {
  state_home: string;
  active_key: string;
  projects: MountedProject[];
}

export interface BindProjectResponse {
  status: string;
  project: MountedProject;
  /** 绑定写的是 active.json；当前进程仍连着旧库，需要重启才生效。 */
  restart_required: boolean;
  message: string;
  warnings: string[];
}

export interface ConstitutionResponse {
  id: string;
  root_id: string | null;
  goal: string;
  target_path: string | null;
  status: string;
  version: number;
  content_hash: string;
  markdown: string;
  data: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  locked_at: string | null;
}

export interface TreeUsage {
  runs?: number;
  input_tokens?: number;
  cached_input_tokens?: number;
  cache_write_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  total_tokens?: number;
  cost_by_currency?: Record<string, number>;
  cost_unlabeled?: number;
}

// 树级债务看板。`oldest_blocking_age_seconds` 为 null 表示"判不出时间戳"，
// 与 `blocking_total === 0`（没被卡）是两件不同的事，界面上不能混成一句。
export interface ObligationBoard {
  blocking_total: number;
  advisory_total: number;
  unsettled_total: number;
  by_type: Record<string, number>;
  by_depth: Record<string, number>;
  oldest_blocking_age_seconds: number | null;
  revalidate_queue_depth: number;
  settled_by_human_count: number;
  settled_total: number;
  recurrence_total: number;
}

// 宪法传播看板。`effective_version` 为 null 表示这棵树还没绑定宪法——与"v0"是两件事。
// `running_on_old_version` 必须单独显示：它是"必须等它跑完"的那部分，
// 与"没人管的落后"完全不同，混在一起会让人以为树卡了。
export interface ConstitutionBoard {
  effective_version: number | null;
  effective_hash: string | null;
  node_total: number;
  version_distribution: Record<string, number>;
  stale_total: number;
  stale_by_action: Record<string, string[]>;
  running_on_old_version: number;
  revalidate_queue_depth: number;
  last_amendment: {
    from_version?: number | null;
    to_version?: number | null;
    direction?: string | null;
    actor?: string | null;
    at?: string | null;
  } | null;
}

// 债务**派生索引**的读面（跨树聚合）。它是 `node_meta` 的缓存、不是事实来源，所以必须同时看
// `built_at` / `in_sync` / `projected`：`in_sync` 为 false = 表已跟真相漂了，`total` 可能偏小；
// `built_at` 为 null 只说明表里一行都没有 —— 配 `projected`（真相里几条）才分得开
// "索引没建过（读不出来）"与"真的一条债都没有"。不可信时的 0 **不代表没有债**。
export interface ObligationIndexBoard {
  status: string;
  total: number;
  by_type: Record<string, number>;
  blocking_by_type: Record<string, number>;
  by_root: Record<string, number>;
  built_at: string | null;
  indexed: number;
  projected: number;
  in_sync: boolean;
  drift_counts: Record<string, number>;
  rebuild_command: string;
}

export interface TreeResponse {
  tasks: BattlefieldTask[];
  edges: Array<{ source: string; target: string }>;
  stats: Record<string, number | string | boolean | null | ObligationBoard | ConstitutionBoard>;
  events: BattlefieldEvent[];
  constitution: ConstitutionResponse | null;
  usage?: TreeUsage;
}

export interface TaskDetailResponse {
  task: BattlefieldTask;
  runs: BattlefieldRun[];
  events: BattlefieldEvent[];
  usage?: TreeUsage;
}

/** 一项可编辑设置（GET /api/settings 的 fields[]）。 */
export interface SettingField {
  key: string;
  env: string;
  label: string;
  kind: 'str' | 'int' | 'float' | 'bool';
  group: string;
  help: string;
  /** 会放宽权限或增加开销 —— 界面要二次确认。 */
  risky: boolean;
  choices: string[];
  minimum: number | null;
  maximum: number | null;
}

/** 一项的当前生效值与来源。source 是 env|config|default。 */
export interface SettingCurrent {
  value: unknown;
  source: 'env' | 'config' | 'default' | string;
  /** 被环境变量压着时为真 —— 此时在界面上改它**不会有效果**。 */
  env_override: boolean;
  file_value: string | null;
}

export interface SettingsResponse {
  groups: Record<string, string>;
  order: string[];
  fields: SettingField[];
  current: Record<string, SettingCurrent>;
  /** 恒为真：Settings 是启动时构造一次的 frozen dataclass。 */
  restart_required: boolean;
  config_file: string;
}

export interface SettingsSaveResponse {
  status: string;
  changed: string[];
  config_file: string;
  restart_required: boolean;
  message: string;
}

/** 一个工程的状态占用（`GET /api/state/overview`）。 */
export interface StateProjectEntry {
  key: string;
  root: string;
  project_dir: string;
  active: boolean;
  root_exists: boolean;
  /** 状态目录与工程是否同盘 —— 跨盘时 git worktree 不可靠。 */
  drive_matches: boolean;
  db_bytes: number;
  /** 整个状态目录（含 runs / worktrees / 快照）。清空重来要删的就是它。 */
  state_bytes: number;
}

export interface StateOverviewResponse {
  state_home: string;
  config_file: string;
  bound: boolean;
  active_key: string;
  project_root: string | null;
  snapshot_matches_active_file: boolean;
  projects: StateProjectEntry[];
  /** 旧 `<repo>/workspace/` 里还剩什么 —— 迁移的入口。 */
  legacy: { present: boolean; db_path: string; db_bytes?: number };
}

/** 迁移预演 / 结果。 */
export interface MigrationPreviewCandidate {
  path: string;
  source: string;
  usable: boolean;
  reason: string;
}

export interface MigrationPreviewResponse {
  available: boolean;
  reason?: string;
  repo_root?: string;
  legacy_db?: string;
  legacy_db_bytes?: number;
  legacy_task_count?: number;
  legacy_worktree_rows?: number;
  current_project_root?: string | null;
  target_key?: string | null;
  candidates?: MigrationPreviewCandidate[];
}

export interface MigrationApplyResponse {
  status: string;
  copied: boolean;
  skipped_reason: string;
  project_key: string;
  target_db: string;
  worktree_rows_rewritten: number;
  codex_home_copied: boolean;
  note: string;
}

export interface StateResetResponse {
  status: string;
  key: string;
  root: string;
  purged: boolean;
  was_active: boolean;
  note: string;
}

/** 一个工程在跨工程总览里的结论（`GET /api/overview/cross-project`）。 */
export type CrossProjectVerdict = 'unknown' | 'blocked' | 'running' | 'idle';

export interface CrossProjectEntry {
  key: string;
  root: string;
  active: boolean;
  root_exists: boolean;
  db_bytes: number;
  state_bytes: number;
  /** false = 这个工程的状态读不到（库不存在/损坏/不是我们的库）。 */
  available: boolean;
  reason: string;
  /** 读不到时这些是 **null 而不是 0** —— "空"与"读不到"必须分得清。 */
  task_total: number | null;
  task_by_status: Record<string, number>;
  roots: { id: string; title: string; status: string; updated_at: string }[];
  spend: Record<string, unknown> | null;
  open_blocking_obligations: number | null;
  latest_event: { type: string; message: string; created_at: string } | null;
  lead_goal: string | null;
  verdict: CrossProjectVerdict;
}

export interface CrossProjectTotals {
  project_count: number;
  readable_count: number;
  task_total: number;
  open_blocking_obligations: number;
  total_tokens: number;
  /** 按币种分桶；一个工程都没有计价数据时是 null（不是 0）。 */
  cost_by_currency: Record<string, number> | null;
  /** 没有币种标签的金额，单列 —— 塞进任何币种桶都是编数据。 */
  cost_unlabeled: number | null;
  blocked_projects: string[];
  unreadable_projects: string[];
  /** true = 合计**偏小**（有工程没读到），界面必须说出来。 */
  totals_incomplete: boolean;
}

export interface CrossProjectResponse {
  state_home: string;
  active_key: string;
  projects: CrossProjectEntry[];
  totals: CrossProjectTotals;
  notes: string[];
}

/** 归因里的一个桶（节点 / 角色 / 模型各一套）。 */
export interface CostBucket {
  key: string;
  runs: number;
  total_tokens: number;
  /** 没有金额时是 null —— 不是 0。 */
  cost_by_currency: Record<string, number> | null;
  /** 只有节点桶有。 */
  title?: string;
  depth?: number | null;
}

export interface CostAttribution {
  runs: number;
  runs_with_usage: number;
  /** 有运行但一条用量都识别不出来（多半是供应商换了字段名）。 */
  runs_without_usage: number;
  runs_unknown_model: number;
  total_tokens: number;
  cost_by_currency: Record<string, number> | null;
  by_kind: CostBucket[];
  by_model: CostBucket[];
  by_node: CostBucket[];
}

/** 一条自动策略的当前判定（`GET /api/auto-policy`，P3-5）。 */
export interface AutoPolicyItem {
  id: string;
  label: string;
  /** 为什么这条策略存在。 */
  rationale: string;
  /** 设置里的开关名 —— 报告里要能看到"这条为什么没触发"。 */
  switch: string;
  enabled: boolean;
  action: string;
  fired: boolean;
  reason: string;
  evidence: Record<string, unknown>;
}

export interface AutoPolicyReport {
  policies: AutoPolicyItem[];
  fired_count: number;
  fired: string[];
  /** 被关掉的策略 —— 这正是"以为有保护其实没有"的那种状态。 */
  disabled: string[];
  note: string;
  root_id?: string | null;
  observed?: Record<string, unknown>;
}

/** 面板上的三态。**关闭优先于触发**（关了就永远不会触发）。 */
export type PolicyStatus = 'disabled' | 'fired' | 'idle';

/** 动作风险目录的一项（`GET /api/steward/pre-authorization`，P4-4）。 */
export interface PreAuthCatalogItem {
  action: string;
  risk: 'low' | 'medium' | 'high';
  label: string;
  rationale: string;
  /** `false` = 高风险，**永远**不能预授权。 */
  pre_authorizable: boolean;
}

export interface PreAuthRiskLevel {
  level: 'low' | 'medium' | 'high';
  label: string;
  rationale: string;
}

export interface PreAuthGrant {
  actions: string[];
  /** 空数组 = 不限目标（但仍受动作集与期限约束）。 */
  targets: string[];
  expires_at: string;
  remaining_seconds: number;
  granted_by: string;
  reason: string;
}

export interface PreAuthorizationResponse {
  catalog: PreAuthCatalogItem[];
  levels: PreAuthRiskLevel[];
  /** `null` = 没有授权（出厂状态：每个动作都要人点）。 */
  grant: PreAuthGrant | null;
  high_risk_never_pre_authorized: boolean;
  note: string;
}

/** 结论文档里由代码抽出的事实（P4-5）。 */
export interface ConclusionFacts {
  root_id?: string;
  node_id?: string;
  node_count?: number;
  settled_count?: number;
  unsettled_count?: number;
  status_counts?: Record<string, number>;
  timeline?: { at: string; type: string; message: string; node_id: string }[];
  timeline_total?: number;
  stuck_nodes?: { id: string; title: string; status: string; error: string; attempts: number }[];
  open_obligations?: { id: string; type: string; node_id: string; severity: string }[];
  cost?: { total_tokens: number; cost_by_currency?: Record<string, number> | null; cost_unlabeled?: number | null };
}

/** `GET /api/conclusions/draft` —— **没有落库**的草稿。 */
export interface ConclusionDraft {
  root_id: string;
  node_id: string;
  title: string;
  facts: ConclusionFacts;
  /** 这份草稿里**没能确定**的地方（空段不等于"没有"）。 */
  incomplete: string[];
  markdown: string;
  note: string;
}

export interface ConclusionPayload {
  id: number;
  root_id: string;
  node_id: string;
  title: string;
  /** `draft` = 事实已抽好等人补；`final` = 人已定稿（**不表示结论正确**）。 */
  status: string;
  author: string;
  authored: string;
  markdown: string;
  facts: ConclusionFacts;
  incomplete: string[];
  created_at: string | null;
  updated_at: string | null;
}

/** 注意账的一条认领（`GET /api/attention`，P4-3）。 */
export interface AttentionClaim {
  id: number;
  kind: string;
  kind_label: string;
  target: string;
  actor: string;
  actor_label: string;
  reason: string;
  claimed_at: string | null;
  expires_at: string | null;
  remaining_seconds: number;
}

export interface AttentionResponse {
  claims: AttentionClaim[];
  count: number;
  actors: { actor: string; label: string }[];
  kinds: { kind: string; label: string }[];
  note: string;
}

/** 可调档位（`GET /api/watchdog/tuning`，P4-2）。 */
export interface TuningCatalogItem {
  key: string;
  env: string;
  label: string;
  kind: 'int' | 'bool';
  minimum: number | null;
  maximum: number | null;
  rationale: string;
  /** `up` / `down`：往哪个方向变才危险；`null` = 未登记方向。 */
  risky_direction: 'up' | 'down' | null;
}

export interface TuningChange {
  key: string;
  label: string;
  old: unknown;
  new: unknown;
  actor: string;
  reason: string;
  risky: boolean;
}

export interface TuningHistoryEntry {
  at: string | null;
  message: string;
  applied: boolean;
  actor: string;
  reason: string;
  changes: TuningChange[];
}

export interface TuningResponse {
  catalog: TuningCatalogItem[];
  current: Record<string, unknown>;
  history: TuningHistoryEntry[];
  note: string;
}

/** 目标投递 + 实时进度（`GET /api/deliveries/latest`，P4-1）。 */
export interface DeliveryRecord {
  id: number;
  root_id: string;
  goal: string;
  author: string;
  source: string;
  created_at: string | null;
}

export interface DeliveryProgress {
  root_id: string | null;
  goal: string;
  author: string;
  /** false = 这棵树读不到（没节点）。**不是**"还没开始"。 */
  available: boolean;
  reason: string;
  done: number;
  total: number;
  /** 分母为 0 时是 null，不是 0 —— 0% 会被读成"一点没做"。 */
  ratio: number | null;
  status_counts?: Record<string, number>;
  frontier: { id: string; title: string; status: string; depth: number }[];
  stuck: { id: string; title: string; status: string; error: string }[];
  blocking_obligations: { id: string; type: string; node_id: string }[];
  next_step: string;
  /** 这份进度里**没能确定**的地方。 */
  incomplete: string[];
}

export interface DeliveryResponse {
  available: boolean;
  reason: string;
  delivery: DeliveryRecord | null;
  progress: DeliveryProgress | null;
  summary?: string;
}

/** 请求指标（`GET /api/metrics`，P5-1 / P5-5）。 */
export interface RouteMetrics {
  key: string;
  count: number;
  errors: number;
  error_rate: number | null;
  last_status: number;
  samples: number;
  total_ms: number;
  mean_ms: number | null;
  max_ms: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
}

export interface MetricsRegression {
  key: string;
  baseline_p95_ms: number;
  current_p95_ms: number;
  ratio: number;
  samples: number;
}

export interface MetricsResponse {
  routes: RouteMetrics[];
  route_count: number;
  sample_limit: number;
  quantiles: number[];
  baseline: Record<string, number>;
  regressions: MetricsRegression[];
  uptime_seconds: number;
  note: string;
}

export interface CostProjection {
  available: boolean;
  /** 不可用时的原因（界面必须显示它，而不是显示 0）。 */
  reason: string;
  currency: string | null;
  unit_cost?: number;
  settled_cost?: number;
  settled_nodes?: number;
  remaining_nodes?: number;
  low?: number;
  mid?: number;
  high?: number;
  sample_nodes?: number;
  retry_factor?: number;
  note?: string;
}

/** `GET /api/cost` 的响应。 */
export interface CostReport {
  root_id: string;
  attribution: CostAttribution;
  projection: CostProjection;
  node_counts: {
    total: number;
    settled: number;
    priced_samples: number;
    remaining: number;
  };
  note: string;
}
