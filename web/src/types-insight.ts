/**
 * 跨工程总览、成本、自动策略、预授权、指标（P5-2：从 `types.ts` 拆出来，纯类型、无逻辑）。
 *
 * `types.ts` 现在是聚合出口（re-export），老调用方不用改。
 */

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

export type PolicyStatus = 'disabled' | 'fired' | 'idle';

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
