import type { BattlefieldTask, ConstitutionBoard, ObligationBoard, ObligationIndexBoard, TaskStatus } from './types';

export const terminalStatuses: TaskStatus[] = ['done', 'blocked', 'failed', 'canceled'];

const imagePathPattern =
  /(?:[A-Za-z]:[^\s`"'<>|]+|(?:\.{0,2}[\\/])?[A-Za-z0-9_. -]+(?:[\\/][A-Za-z0-9_. -]+)+)\.(?:png|jpe?g|webp|gif)/gi;
const imageExtensionPattern = /\.(?:png|jpe?g|webp|gif)(?:$|[?#\s`'")\]}>,;])/i;
const finalOutcomePattern = /final|product|gate|snapshot|screenshot|preview|visual|render|成果|收束|验收|截图|预览|可玩/i;

export type TaskOutcomePreview =
  | {
      kind: 'image';
      label: string;
      title: string;
      imagePath: string;
      imageUrl: string;
    }
  | {
      kind: 'artifact';
      label: string;
      title: string;
    };

export function isActiveFrontier(task: BattlefieldTask, parentIds: Set<string>): boolean {
  return !parentIds.has(task.id) && !terminalStatuses.includes(task.status);
}

export function summarizeStats(tasks: BattlefieldTask[]) {
  const parentIds = new Set(tasks.map((task) => task.parent_id).filter((id): id is string => Boolean(id)));
  return {
    total: tasks.length,
    active_frontier: tasks.filter((task) => isActiveFrontier(task, parentIds)).length,
    max_depth: Math.max(0, ...tasks.map((task) => task.depth)),
  };
}

function isObligationBoard(value: unknown): value is ObligationBoard {
  return !!value && typeof value === 'object' && 'blocking_total' in value;
}

function isConstitutionBoard(value: unknown): value is ConstitutionBoard {
  return !!value && typeof value === 'object' && 'effective_version' in value;
}

export function obligationBoardFromStats(
  stats: Record<string, number | string | boolean | null | ObligationBoard | ConstitutionBoard> | undefined,
): ObligationBoard | null {
  const board = stats?.obligations;
  return isObligationBoard(board) ? board : null;
}

export function constitutionBoardFromStats(
  stats: Record<string, number | string | boolean | null | ObligationBoard | ConstitutionBoard> | undefined,
): ConstitutionBoard | null {
  const board = stats?.constitution;
  return isConstitutionBoard(board) ? board : null;
}

// 「落后」要分两种说：执行中落后的那部分必须等它跑完，其余是真的需要处理的。
// 混成一句会让人以为树卡住了，或者以为它在干活——两个误判方向相反，代价都真实。
export function constitutionPropagationHeadline(board: ConstitutionBoard): string {
  if (board.effective_version === null) return '未绑定宪法';
  if (board.stale_total === 0) return `全部节点在 v${board.effective_version}`;
  const versions = Object.keys(board.version_distribution).length;
  const parts = [`${board.stale_total} 个节点落后`];
  if (board.running_on_old_version > 0) parts.push(`其中 ${board.running_on_old_version} 个执行中必须等`);
  if (board.revalidate_queue_depth > 0) parts.push(`待再验证 ${board.revalidate_queue_depth}`);
  if (versions > 1) parts.push(`${versions} 个版本并存`);
  return parts.join(' · ');
}

export function versionDistributionEntries(board: ConstitutionBoard): Array<{ version: string; count: number }> {
  return Object.entries(board.version_distribution)
    .sort(([left], [right]) => left.localeCompare(right, undefined, { numeric: true }))
    .map(([version, count]) => ({ version, count }));
}

const obligationTypeLabels: Record<string, string> = {
  child_blocked: '子节点阻塞',
  child_failed: '子节点失败',
  integration_conflict: '集成冲突',
  integration_skipped: '集成跳过',
  constitution_stale: '宪法版本落后',
  revalidate_required: '待再验证',
  // 下面几条与 `api/app/obligations.py` 的类型常量一一对应；表里**没有**的键一律回退成原名
  // （宁可显示英文标识，也不要贴一个自己编的中文名——那会把两种不同的债说成一种）。
  constraint_coverage_gap: '约束覆盖缺口',
  regression_break: '跨版本回归',
  acceptance_surface_tampered: '验收面被改动',
  requirement_uncovered: '需求没有判据',
  requirement_unmet: '需求没做到',
  cross_node_leak: '跨节点泄露',
};

// 「卡了多久」必须与「卡在哪」分开说：只报一个总数会让人以为树在干活。
// 判不出时间戳时明说判不出，不用 0 秒冒充刚记上。
export function obligationBoardHeadline(board: ObligationBoard): string {
  if (board.blocking_total === 0 && board.advisory_total === 0) return '无未清债务';
  const parts: string[] = [];
  if (board.blocking_total > 0) parts.push(`阻断 ${board.blocking_total}`);
  if (board.advisory_total > 0) parts.push(`提示 ${board.advisory_total}`);
  if (board.oldest_blocking_age_seconds !== null) {
    parts.push(`最久 ${formatObligationAge(board.oldest_blocking_age_seconds)}`);
  } else if (board.blocking_total > 0) {
    parts.push('卡了多久：判不出');
  }
  return parts.join(' · ');
}

export function formatObligationAge(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时`;
  return `${Math.floor(seconds / 86400)} 天`;
}

export function obligationTypeLabel(type: string): string {
  return obligationTypeLabels[type] ?? type;
}

export function obligationTypeBreakdown(board: ObligationBoard): Array<{ type: string; label: string; count: number }> {
  return Object.entries(board.by_type)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([type, count]) => ({ type, label: obligationTypeLabel(type), count }));
}

// 索引是**缓存**：`in_sync` 为 false = 已跟 node_meta 漂了，`total` 不构成读数。
// `built_at` 为 null 只说明**表里一行都没有**，它有两种完全相反的含义，必须靠 `projected`
// （真相里有几条债）分开：`projected > 0` = 索引没建过/被清过（读不出来）；`projected === 0`
// 且没漂 = **真的一条债都没有**。早先把 built_at===null 一律判成不可读，结果是干净的树永远
// 显示"索引未建，读不出来"，而"确实清干净了"这个状态根本显示不出来（审计修正 2026-09-24）。
export function obligationIndexIsReadable(board: ObligationIndexBoard): boolean {
  if (!board.in_sync) return false;
  return board.built_at !== null || board.projected === 0;
}

export function obligationIndexScopeLabel(status: string): string {
  if (status === 'all') return '全部';
  if (status === 'settled') return '已清';
  return '未清';
}

export function obligationIndexDriftSummary(board: ObligationIndexBoard): string {
  const labels: Array<[string, string]> = [
    ['missing', '缺'],
    ['extra', '多'],
    ['changed', '变'],
  ];
  return labels
    .filter(([key]) => (board.drift_counts[key] ?? 0) > 0)
    .map(([key, label]) => `${label} ${board.drift_counts[key]}`)
    .join(' · ');
}

// 「读不出来」与「读出来是 0」必须分开说：把前者显示成 0 就是让人误以为没有债。
export function obligationIndexHeadline(board: ObligationIndexBoard): string {
  const scope = obligationIndexScopeLabel(board.status);
  // 表空 + 真相里有债 = 没建过/被清过，这时先说"读不出来"（比说"过期"更准）。
  if (board.built_at === null && board.projected > 0) {
    return `索引未建：跨树${scope}债务读不出来（不是没有债）`;
  }
  if (!board.in_sync) {
    const drift = obligationIndexDriftSummary(board);
    return `索引已过期（${drift}）：跨树${scope} ${board.total} 只是旧读数`;
  }
  return `跨树${scope} ${board.total}`;
}

export function obligationIndexTypeBreakdown(
  board: ObligationIndexBoard,
): Array<{ type: string; label: string; count: number }> {
  return Object.entries(board.by_type)
    .sort(([, left], [, right]) => right - left)
    .map(([type, count]) => ({ type, label: obligationTypeLabel(type), count }));
}

export function outcomePreviewForTask(task: BattlefieldTask): TaskOutcomePreview | null {
  if (task.status !== 'done') return null;
  const imagePath = firstImagePathForTask(task);
  if (imagePath) {
    return {
      kind: 'image',
      label: '截图',
      title: imagePath,
      imagePath,
      imageUrl: `/api/files/preview?path=${encodeURIComponent(imagePath)}`,
    };
  }
  if (!hasHighValueOutcome(task)) return null;
  return {
    kind: 'artifact',
    label: '成果',
    title: outcomeTitle(task),
  };
}

function firstImagePathForTask(task: BattlefieldTask): string | null {
  const candidates = collectOutcomeStrings(task);
  for (const candidate of candidates) {
    const direct = normalizeImageCandidate(candidate, task);
    if (direct) return direct;
    for (const match of candidate.matchAll(imagePathPattern)) {
      const normalized = normalizeImageCandidate(match[0], task);
      if (normalized) return normalized;
    }
  }
  return null;
}

function normalizeImageCandidate(raw: string, task: BattlefieldTask): string | null {
  let value = raw.trim().replace(/^`+|`+$/g, '').replace(/^["']+|["']+$/g, '');
  value = value.replace(/[.,;:，。；：、)\]}]+$/g, '');
  if (!imageExtensionPattern.test(`${value} `)) return null;
  if (/^https?:\/\//i.test(value)) return null;
  if (value.startsWith('res://')) {
    value = value.slice('res://'.length);
  }
  value = value.replace(/^\.[\\/]/, '');
  if (/^[A-Za-z]:[\\/]/.test(value)) {
    return value;
  }
  if (task.worktree_path) {
    return `${task.worktree_path.replace(/[\\/]$/, '')}\\${value.replace(/^[\\/]/, '')}`;
  }
  return value;
}

function collectOutcomeStrings(task: BattlefieldTask): string[] {
  const result = asRecord(task.result);
  const nodeMeta = asRecord(task.node_meta);
  return [
    ...collectStrings(result?.worker),
    ...collectStrings(result?.acceptance),
    ...collectStrings(result?.verifier),
    ...collectStrings(nodeMeta?.outcome_preview),
    ...collectStrings(nodeMeta?.artifact_preview),
  ].slice(0, 240);
}

function hasHighValueOutcome(task: BattlefieldTask): boolean {
  const result = asRecord(task.result);
  if (!result) return false;
  const worker = asRecord(result.worker);
  const acceptance = asRecord(result.acceptance);
  const hasChangedFiles = stringList(worker?.changed_files).length > 0;
  const hasEvidence = stringList(worker?.evidence).length > 0 || stringList(acceptance?.evidence).length > 0;
  const accepted = String(acceptance?.verdict ?? '') === 'pass';
  const workerSucceeded = String(worker?.status ?? '') === 'succeeded';
  const highValueText = `${task.title} ${task.description}`;
  return (hasChangedFiles || hasEvidence || accepted || workerSucceeded) && (task.depth <= 2 || finalOutcomePattern.test(highValueText));
}

function outcomeTitle(task: BattlefieldTask): string {
  const result = asRecord(task.result);
  const worker = asRecord(result?.worker);
  const acceptance = asRecord(result?.acceptance);
  const changedCount = stringList(worker?.changed_files).length;
  const evidenceCount = stringList(worker?.evidence).length + stringList(acceptance?.evidence).length;
  if (changedCount > 0 && evidenceCount > 0) return `${changedCount} 个文件，${evidenceCount} 条证据`;
  if (changedCount > 0) return `${changedCount} 个交付文件`;
  if (evidenceCount > 0) return `${evidenceCount} 条验收证据`;
  return '成功节点成果';
}

function collectStrings(value: unknown, out: string[] = []): string[] {
  if (typeof value === 'string') {
    out.push(value);
    return out;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectStrings(item, out));
    return out;
  }
  const record = asRecord(value);
  if (record) {
    Object.values(record).forEach((item) => collectStrings(item, out));
  }
  return out;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}
