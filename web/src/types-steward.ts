/**
 * 管家消息与动作（P5-2：从 `types.ts` 拆出来，纯类型、无逻辑）。
 *
 * `types.ts` 现在是聚合出口（re-export），老调用方不用改。
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

export interface StewardAlertPush {
  alerts: StewardAlert[];
  worth_pushing: StewardAlert[];
  counts: Record<string, number>;
  /** 为什么现在推。用户最想知道的是"这跟上次有何不同"。 */
  reason: string;
  /** 当前的打扰阈值，让界面能解释"为什么这条没弹"。 */
  min_level: string;
}

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
