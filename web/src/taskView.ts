/**
 * 从 `App.tsx` 拆出来的：17 个声明（P5-2：单文件不再超 500 行）。
 *
 * 抽出来是为了让 `App.tsx` 回到 500 行以内；这里只搬家，没有改行为。
 */
import { statusLabel } from './labels';
import { BattlefieldTask, TaskStatus, TreeResponse } from './types';

export const edgeStateStyles: Record<TaskStatus, { className: string; color: string; animated: boolean }> = {
  planned: { className: 'planned', color: '#778695', animated: false },
  planning: { className: 'active', color: '#357c9b', animated: true },
  decomposing: { className: 'active', color: '#357c9b', animated: true },
  queued: { className: 'queued', color: '#778695', animated: false },
  waiting_children: { className: 'waiting', color: '#5f7888', animated: false },
  running: { className: 'running', color: '#24789c', animated: true },
  working: { className: 'running', color: '#24789c', animated: true },
  verifying: { className: 'verifying', color: '#7b58a8', animated: true },
  accepting: { className: 'accepting', color: '#7b58a8', animated: true },
  done: { className: 'done', color: '#34895a', animated: false },
  repair: { className: 'repair', color: '#b97828', animated: true },
  blocked: { className: 'blocked', color: '#b94747', animated: false },
  failed: { className: 'blocked', color: '#b94747', animated: false },
  paused: { className: 'paused', color: '#7f8b95', animated: false },
  hibernating: { className: 'hibernating', color: '#6d7b88', animated: false },
  canceled: { className: 'canceled', color: '#7f8b95', animated: false },
};

export const liveNodeStatuses = new Set<TaskStatus>([
  'planning',
  'decomposing',
  'running',
  'working',
  'verifying',
  'accepting',
]);

export type NodeLod = 'full' | 'compact' | 'micro';

export function nodeLodForZoom(zoom: number): NodeLod {
  if (zoom < 0.34) return 'micro';
  if (zoom < 0.58) return 'compact';
  return 'full';
}

export type TaskNodeData = {
  task: BattlefieldTask;
  selected: boolean;
  isParent: boolean;
  isCollapsed: boolean;
  hiddenDescendantCount: number;
  archivedDoneCount: number;
  archivedDoneRootCount: number;
  archivedDoneExpanded: boolean;
  lod: NodeLod;
  waitingReport: Record<string, unknown> | null;
  taskById: Map<string, BattlefieldTask>;
  onToggleCollapse: (task: BattlefieldTask) => void;
  onToggleDoneArchive: (task: BattlefieldTask) => void;
};

export type ArtifactKind = 'prompt' | 'logs' | 'last_message';

export function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

export function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item))
    : [];
}

export function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

export function nodeBlockSummary(task: BattlefieldTask, taskById?: Map<string, BattlefieldTask>, seen: Set<string> = new Set()): string | null {
  if (seen.has(task.id)) {
    return null;
  }
  seen.add(task.id);
  const ownReport = asRecord(task.node_meta.block_report);
  const dependencyReports = asRecordArray(ownReport?.dependency_reports);
  if (dependencyReports.length > 0) {
    const summaries = dependencyReports.slice(0, 2).map((report) => {
      const title = String(report.title ?? report.task_id ?? '依赖节点');
      const status = String(report.status_label ?? report.status ?? '未完成');
      const reason = String(report.reason ?? '暂无阻塞原因');
      return `依赖「${title}」${status}：${reason}`;
    });
    return summaries.join('；');
  }

  const reportSource = String(ownReport?.source ?? '');
  const ownReason = ownReport ? String(ownReport.reason ?? '').trim() : '';
  const isDependencyBlock = reportSource === 'dependency.blocked' || ownReason.includes('依赖节点未成功完成');
  if (isDependencyBlock) {
    const dependencyIds = stringList(ownReport?.dependencies).length > 0 ? stringList(ownReport?.dependencies) : stringList(task.node_meta.depends_on_ids);
    const dependencySummaries = dependencyIds
      .map((dependencyId) => {
        const dependency = taskById?.get(dependencyId);
        if (!dependency) {
          return `依赖「${dependencyId}」未找到或未加载`;
        }
        const traced = nodeBlockSummary(dependency, taskById, new Set(seen));
        const fallback = dependency.error_message || (dependency.status === 'done' ? '已完成' : `状态为 ${statusLabel(dependency.status) ?? dependency.status}`);
        return `依赖「${dependency.title}」${statusLabel(dependency.status) ?? dependency.status}：${traced ?? fallback}`;
      })
      .filter((item) => item.length > 0);
    if (dependencySummaries.length > 0) {
      return dependencySummaries.join('；');
    }
  }

  const childReports = asRecordArray(task.node_meta.blocked_child_reports);
  if (childReports.length > 0) {
    const firstReport = childReports[0];
    const title = String(firstReport.title ?? firstReport.task_id ?? '子节点');
    const reason = String(firstReport.reason ?? '暂无阻塞原因');
    const retryLabel = firstReport.retryable ? '可等待重试' : '需人工处理';
    return `${retryLabel}：子任务「${title}」阻塞：${reason}`;
  }

  if (ownReason) {
    const retryLabel = ownReport?.retryable ? '可重试' : '需处理';
    return `${retryLabel}：${ownReason}`;
  }
  if (task.error_message && ['blocked', 'failed', 'repair'].includes(task.status)) {
    return task.error_message;
  }
  return null;
}

export function readStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === 'string');
}

export function taskEvidenceBadges(task: BattlefieldTask): { evidence: number; changed: number; tests: number } {
  const result = (task.result ?? {}) as Record<string, unknown>;
  const acceptance = (result.acceptance ?? {}) as Record<string, unknown>;
  return {
    evidence: readStringList(result.evidence).length + readStringList(acceptance.evidence).length,
    changed: readStringList(result.changed_files).length,
    tests: readStringList(result.tests).length,
  };
}

export function summarizeBlockedNodes(tasks: BattlefieldTask[]): {
  count: number;
  top: string | null;
  topTaskId: string | null;
  topTaskTitle: string | null;
} | null {
  const blocked = tasks.filter((task) => task.status === 'blocked' || task.status === 'failed');
  if (!blocked.length) {
    return null;
  }
  const taskById = new Map(tasks.map((task) => [task.id, task]));
  // 顺序保持稳定（tasks 的顺序来自后端），避免看板上的首要原因来回跳动。
  for (const task of blocked) {
    const reason = nodeBlockSummary(task, taskById);
    if (reason) {
      return { count: blocked.length, top: reason, topTaskId: task.id, topTaskTitle: task.title };
    }
  }
  return { count: blocked.length, top: null, topTaskId: null, topTaskTitle: null };
}

export function buildWaitingReports(tasks: BattlefieldTask[]): Map<string, Record<string, unknown>> {
  const childrenByParent = new Map<string | null, BattlefieldTask[]>();
  tasks.forEach((task) => {
    const children = childrenByParent.get(task.parent_id) ?? [];
    children.push(task);
    childrenByParent.set(task.parent_id, children);
  });

  const descendantsOf = (taskId: string): BattlefieldTask[] => {
    const result: BattlefieldTask[] = [];
    const stack = [...(childrenByParent.get(taskId) ?? [])].reverse();
    while (stack.length > 0) {
      const child = stack.pop();
      if (!child) continue;
      result.push(child);
      stack.push(...[...(childrenByParent.get(child.id) ?? [])].reverse());
    }
    return result;
  };

  const countStatus = (items: BattlefieldTask[], status: TaskStatus) => items.filter((item) => item.status === status).length;
  const reports = new Map<string, Record<string, unknown>>();
  tasks.forEach((task) => {
    const directChildren = childrenByParent.get(task.id) ?? [];
    if (task.status !== 'waiting_children' || directChildren.length === 0) return;
    const descendants = descendantsOf(task.id);
    const activeLeaves = descendants.filter((item) => liveNodeStatuses.has(item.status));
    const blockedNodes = descendants.filter((item) => item.status === 'blocked' || item.status === 'failed' || item.status === 'repair');
    const directDone = countStatus(directChildren, 'done');
    reports.set(task.id, {
      role: 'reporting_parent',
      summary: `父节点等待子树收敛：直属子任务 ${directDone}/${directChildren.length} 完成，真实运行叶子 Agent ${activeLeaves.length} 个，排队 ${countStatus(descendants, 'queued')} 个，阻塞/修复 ${blockedNodes.length} 个。`,
      direct_total: directChildren.length,
      direct_done: directDone,
      direct_blocked:
        countStatus(directChildren, 'blocked') + countStatus(directChildren, 'failed') + countStatus(directChildren, 'repair'),
      subtree_total: descendants.length,
      subtree_done: countStatus(descendants, 'done'),
      queued_descendants: countStatus(descendants, 'queued'),
      waiting_parent_descendants: countStatus(descendants, 'waiting_children'),
      blocked_descendants: blockedNodes.length,
      active_agents: activeLeaves.length,
      active_leaves: activeLeaves.slice(0, 6).map((leaf) => ({
        id: leaf.id,
        title: leaf.title,
        status: leaf.status,
        depth: leaf.depth,
      })),
      blocked_nodes: blockedNodes.slice(0, 6).map((node) => ({
        id: node.id,
        title: node.title,
        status: node.status,
        reason: node.error_message ?? '',
      })),
      updated_at: task.updated_at,
    });
  });
  return reports;
}

export function parseJsonObject(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('宪法结构 JSON 必须是对象。');
  }
  return value as Record<string, unknown>;
}

export type TreeStats = TreeResponse['stats'];
