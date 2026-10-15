import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type Viewport,
} from '@xyflow/react';
import {
  Activity,
  Archive,
  Ban,
  ChevronDown,
  ChevronRight,
  ChevronsDown,
  ChevronsUp,
  Clock,
  FileText,
  Layers,
  Maximize2,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
  Terminal,
  Zap,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  cancelAllProjects,
  draftConstitution,
  fetchLatestConstitution,
  fetchObligationIndex,
  fetchRunArtifact,
  fetchStateLayout,
  fetchTask,
  fetchTree,
  fetchWatchdogState,
  installWatchdog,
  lockConstitution,
  postTaskAction,
  runWatchdogOnce,
  setApiRestartDrain,
  setBudgetGuard,
  startOneShotProject,
  startProject,
  updateConstitution,
} from './api';
import { layoutBattlefield } from './layout';
import { StewardPanel } from './StewardPanel';
import {
  constitutionBoardFromStats,
  constitutionPropagationHeadline,
  obligationBoardFromStats,
  obligationBoardHeadline,
  obligationIndexHeadline,
  obligationIndexIsReadable,
  obligationIndexTypeBreakdown,
  obligationTypeBreakdown,
  outcomePreviewForTask,
  versionDistributionEntries,
} from './model';
import type {
  BattlefieldEvent,
  BattlefieldTask,
  ConstitutionBoard,
  ConstitutionResponse,
  GuardianCheck,
  ObligationBoard,
  ObligationIndexBoard,
  RunArtifactResponse,
  TaskDetailResponse,
  TaskStatus,
  TreeResponse,
  TreeUsage,
  WatchdogActionResponse,
  StateLayoutResponse,
  WatchdogStateResponse,
} from './types';

type TreeStats = TreeResponse['stats'];

function statNumber(stats: TreeStats | null | undefined, key: string, fallback = 0): number {
  const value = stats?.[key];
  return typeof value === 'number' ? value : fallback;
}

function statText(stats: TreeStats | null | undefined, key: string, fallback = '默认'): string {
  const value = stats?.[key];
  return typeof value === 'string' ? value : fallback;
}

function displaySlotCount(activeSlots: number, capacity: number): number {
  const safeActive = Math.max(0, activeSlots);
  const safeCapacity = Math.max(0, capacity);
  return safeCapacity > 0 ? Math.min(safeActive, safeCapacity) : safeActive;
}

function autoConcurrencyLabel(mode: string): string {
  return (
    {
      high_throughput: '高峰',
      balanced: '均衡',
      low_power: '低功率',
      hibernate_low_power: '休眠',
      resource_cooldown: '冷却',
      budget_preserve: '保留',
      fixed_single_agent: '固定并发',
      fixed_capacity: '固定并发',
      manual: '手动',
      pending: '待评估',
    }[mode] ?? mode
  );
}

const watchdogActionLabels: Record<string, string> = {
  budget_guard_sleep: '预算守卫',
  resume: '到点恢复',
  resume_orphaned: '复活调度',
  observe_running: '运行观察',
  wait_for_resume_at: '等待自启',
  observe: '持续观察',
  idle: '空闲',
};

const guardianStatusLabels: Record<string, string> = {
  watching: '监护中',
  low_power: '低功耗',
  sleeping: '休眠守护',
  recovery_ready: '恢复就绪',
  ready: '可继续',
  needs_attention: '需要关注',
  idle: '空闲',
  unknown: '等待状态',
};

const guardianRiskLabels: Record<string, string> = {
  ok: '正常',
  watch: '观察',
  warning: '预警',
  sleeping: '休眠',
  critical: '高风险',
};

const guardianCheckLabels: Record<string, string> = {
  ok: '正常',
  warning: '预警',
  critical: '高风险',
  sleeping: '休眠',
  idle: '空闲',
};

function formatDateTime(value: string | null | undefined): string {
  if (!value) return '未排程';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function secondsUntil(value: string | null | undefined): number | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return Math.max(0, Math.ceil((date.getTime() - Date.now()) / 1000));
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '未排程';
  const safeSeconds = Math.max(0, seconds);
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  if (hours > 0) return `${hours} 小时 ${minutes} 分钟`;
  return `${minutes} 分钟`;
}

function formatPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return 'n/a';
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

function summarizeWatchdogAction(result: WatchdogActionResponse): string {
  const text = [result.stdout, result.stderr].join('\n').trim();
  const lastLine = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).slice(-1)[0];
  const label = result.status === 'succeeded' ? '成功' : '失败';
  const code = result.exit_code === null ? '无退出码' : `退出码 ${result.exit_code}`;
  return `${label}，${code}${lastLine ? `：${lastLine}` : ''}`;
}

function blockParentActionLabel(value: unknown): string {
  switch (String(value ?? 'manual_review')) {
    case 'wait_and_retry':
      return '等待池子恢复后重试';
    case 'recursive_recovery':
      return '转为递归修复';
    case 'recursive_recovery_static_wrapper':
      return '递归修复：改用静态 wrapper/替代验证';
    case 'skill_lookup_recovery':
      return '先用 Skill/KM 自救';
    case 'human_auth_required':
      return '人工授权/登录';
    case 'human_visual_review':
      return '人工视觉/审美验收';
    case 'pause_tool_fix':
      return '暂停：等待工具自身修复';
    case 'manual_review':
      return '人工判断';
    default:
      return String(value);
  }
}

function blockRetryableLabel(value: unknown, parentAction: unknown): string {
  if (value) return '是，等待后可重试';
  const action = String(parentAction ?? '');
  if (action === 'pause_tool_fix') return '否，工具修复后自动恢复';
  if (action === 'skill_lookup_recovery') return '否，先尝试 Skill/KM 自救';
  if (action === 'human_auth_required') return '否，需要人工登录或授权';
  if (action === 'human_visual_review') return '否，需要人工视觉判断';
  if (action === 'recursive_recovery' || action === 'recursive_recovery_static_wrapper') return '否，交给递归修复';
  return '否，需要人工审查';
}

const DEFAULT_GOAL =
  '通过递归拆解未知深度的开发任务，制作一个可玩的小游戏原型；每个叶子任务都必须执行并通过验收。';

const statusLabels: Record<TaskStatus, string> = {
  planned: '已规划',
  planning: '规划中',
  decomposing: '拆解中',
  queued: '排队中',
  waiting_children: '等待子任务',
  running: '执行中',
  working: '执行中',
  verifying: '验收中',
  accepting: '整合验收',
  done: '已完成',
  repair: '待修复',
  blocked: '已阻塞',
  failed: '失败',
  paused: '已暂停',
  hibernating: '休眠中',
  canceled: '已取消',
};

const runKindLabels: Record<string, string> = {
  constitution: '项目宪法',
  planner: '规划',
  worker: '执行',
  verifier: '验收',
  acceptance: '父节点验收',
};

const runStatusLabels: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
};

const eventTypeLabels: Record<string, string> = {
  'project.started': '项目启动',
  'constitution.run': '宪法运行',
  'constitution.drafted': '宪法草案',
  'constitution.updated': '宪法更新',
  'constitution.locked': '宪法锁定',
  'planner.decision': '规划决策',
  'planner.corrected': '规划修正',
  'planner.repair_converted': '规划修复转递归',
  'planner.blocked_converted': '规划阻塞转递归',
  'acceptance.blocked_converted': '整合阻塞转递归',
  'task.created': '子任务创建',
  'task.retry': '重新排队',
  'task.planned': '已规划',
  'task.decomposing': '拆解中',
  'task.planning': '规划中',
  'task.queued': '排队中',
  'task.waiting_children': '等待子任务',
  'task.running': '执行中',
  'task.working': '执行中',
  'task.verifying': '验收中',
  'task.accepting': '整合验收',
  'task.done': '已完成',
  'task.leaf_misclassified': '叶子转父',
  'task.repeated_split_converted': '重复拆分破环',
  'parent.integration': '父节点整合',
  'task.repair': '待修复',
  'task.blocked': '已阻塞',
  'child.blocked_report': '子节点阻塞上报',
  'parent.child_block_reports': '父节点收到阻塞',
  'parent.retry_blocked_children': '父节点重试子节点',
  'task.failed': '失败',
  'task.paused': '已暂停',
  'task.hibernating': '休眠中',
  'task.hibernate_resume': '休眠恢复',
  'tree.hibernate': '任务树休眠',
  'tree.hibernate_resume': '任务树恢复',
  'task.canceled': '已取消',
  'worktree.created': '工作树创建',
  'worktree.fallback': '工作树降级',
};

function eventSeverity(type: string): 'danger' | 'warn' | 'ok' | 'info' {
  if (type.includes('failed') || type.includes('blocked')) {
    return 'danger';
  }
  if (
    type.includes('hibernate') ||
    type.includes('misclassified') ||
    type.includes('repair') ||
    type.includes('canceled')
  ) {
    return 'warn';
  }
  if (type.endsWith('.done') || type.includes('resume') || type.includes('locked')) {
    return 'ok';
  }
  return 'info';
}

function formatEventClock(createdAt: string): string {
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

const edgeStateStyles: Record<TaskStatus, { className: string; color: string; animated: boolean }> = {
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

const liveNodeStatuses = new Set<TaskStatus>([
  'planning',
  'decomposing',
  'running',
  'working',
  'verifying',
  'accepting',
]);

const nodeTypes = {
  task: TaskNodeView,
};

function rootIdFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('root_id');
}

function writeRootIdToUrl(rootId: string | null) {
  const url = new URL(window.location.href);
  if (rootId) {
    url.searchParams.set('root_id', rootId);
  } else {
    url.searchParams.delete('root_id');
  }
  window.history.replaceState({}, '', url);
}

function previewUrlForRoot(rootId: string): string {
  const url = new URL(window.location.href);
  url.searchParams.set('root_id', rootId);
  return url.toString();
}

function openPendingPreviewWindow(): Window | null {
  try {
    const previewWindow = window.open('', '_blank');
    if (!previewWindow) return null;
    previewWindow.document.title = 'L4 Navigator';
    previewWindow.document.body.innerHTML = `
      <main style="min-height:100vh;display:grid;place-items:center;margin:0;background:#f4f8fb;color:#102033;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
        <section style="text-align:center;">
          <div style="width:220px;height:6px;overflow:hidden;border-radius:999px;background:#d9e7ef;margin:0 auto 18px;">
            <div style="width:45%;height:100%;border-radius:999px;background:#1f7a93;animation:move 1.1s infinite ease-in-out;"></div>
          </div>
          <h1 style="font-size:20px;margin:0 0 8px;">L4 导航正在规划任务树</h1>
          <p style="font-size:14px;margin:0;color:#526577;">创建完成后会自动切换到可视化导航视图。</p>
        </section>
        <style>@keyframes move{0%{transform:translateX(-120%)}100%{transform:translateX(240%)}}</style>
      </main>
    `;
    return previewWindow;
  } catch {
    return null;
  }
}

function navigatePreviewWindow(previewWindow: Window | null, url: string) {
  if (previewWindow) {
    previewWindow.location.href = url;
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}

type NodeLod = 'full' | 'compact' | 'micro';

function nodeLodForZoom(zoom: number): NodeLod {
  if (zoom < 0.34) return 'micro';
  if (zoom < 0.58) return 'compact';
  return 'full';
}

type TaskNodeData = {
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

type ArtifactKind = 'prompt' | 'logs' | 'last_message';

export function App() {
  const [tree, setTree] = useState<TreeResponse | null>(null);
  const [watchdogState, setWatchdogState] = useState<WatchdogStateResponse | null>(null);
  // 当前进程在用的工程/库/状态根（`/api/state`）。与任务树无关，所以不跟 rootId 走。
  const [layoutState, setLayoutState] = useState<StateLayoutResponse | null>(null);
  const [obligationIndex, setObligationIndex] = useState<ObligationIndexBoard | null>(null);
  const [detail, setDetail] = useState<TaskDetailResponse | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [rootId, setRootId] = useState<string | null>(() => rootIdFromUrl());
  const [goal, setGoal] = useState(DEFAULT_GOAL);
  const [targetPath, setTargetPath] = useState('');
  const [runnerMode, setRunnerMode] = useState<'mock' | 'real'>('mock');
  const [constitutionDraft, setConstitutionDraft] = useState<ConstitutionResponse | null>(null);
  const [constitutionMarkdown, setConstitutionMarkdown] = useState('');
  const [constitutionDataText, setConstitutionDataText] = useState('');
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all' | 'active'>('all');
  const [collapsedNodeIds, setCollapsedNodeIds] = useState<Set<string>>(() => new Set());
  const [compactDoneArchive] = useState(true);
  const [expandedDoneArchiveParentIds, setExpandedDoneArchiveParentIds] = useState<Set<string>>(() => new Set());
  const [visibleDepthLimit, setVisibleDepthLimit] = useState<number | null>(null);
  const [nodeLod, setNodeLod] = useState<NodeLod>('full');
  const [nodes, setNodes] = useState<Node[]>([]);
  const [busy, setBusy] = useState(false);
  const [busyLabel, setBusyLabel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [watchdogResult, setWatchdogResult] = useState<string | null>(null);
  const treeLoadInFlight = useRef(false);

  const applyConstitution = useCallback((record: ConstitutionResponse | null) => {
    setConstitutionDraft(record);
    setConstitutionMarkdown(record?.markdown ?? '');
    setConstitutionDataText(record ? JSON.stringify(record.data, null, 2) : '');
  }, []);

  const loadTree = useCallback(async (overrideRootId = rootId) => {
    if (treeLoadInFlight.current) {
      return;
    }
    treeLoadInFlight.current = true;
    try {
      const next = await fetchTree(overrideRootId, {
        compactDone: compactDoneArchive,
        expandedDoneParentIds: compactDoneArchive ? [...expandedDoneArchiveParentIds] : [],
      });
      setTree(next);
      // 布局与任务树无关：即使树是空的也要能显示"我在哪个工程"（未绑定时尤其重要）。
      fetchStateLayout().then(setLayoutState).catch(() => setLayoutState(null));
      const nextRootId = next.tasks.find((task) => task.parent_id === null)?.id ?? overrideRootId ?? null;
      if (nextRootId) {
        fetchWatchdogState(nextRootId).then(setWatchdogState).catch(() => setWatchdogState(null));
      } else {
        setWatchdogState(null);
      }
      // 跨树债务索引与当前看的是哪棵树无关（它是全库缓存），所以不跟 rootId 走。
      fetchObligationIndex().then(setObligationIndex).catch(() => setObligationIndex(null));
      const selectedStillExists = selectedId ? next.tasks.some((task) => task.id === selectedId) : false;
      if (!selectedStillExists) {
        setSelectedId(next.tasks[0]?.id ?? null);
      }
    } finally {
      treeLoadInFlight.current = false;
    }
  }, [compactDoneArchive, expandedDoneArchiveParentIds, rootId, selectedId]);

  useEffect(() => {
    loadTree().catch((err: unknown) => setError(String(err)));
    const interval = window.setInterval(() => loadTree().catch(() => undefined), 10000);
    const source = new EventSource('/api/events');
    let eventRefreshTimer: number | null = null;
    source.onmessage = () => {
      if (eventRefreshTimer !== null) {
        window.clearTimeout(eventRefreshTimer);
      }
      eventRefreshTimer = window.setTimeout(() => {
        eventRefreshTimer = null;
        loadTree().catch(() => undefined);
      }, 2000);
    };
    return () => {
      window.clearInterval(interval);
      if (eventRefreshTimer !== null) {
        window.clearTimeout(eventRefreshTimer);
      }
      source.close();
    };
  }, [loadTree]);

  useEffect(() => {
    const onPopState = () => {
      setRootId(rootIdFromUrl());
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  useEffect(() => {
    // 拿到 root 之后就按 root 读：多棵树并存时后端会拒答不带 root 的请求（不猜），
    // 而猜错的那一条会让界面把树误判成"宪法陈旧"。首屏还没 root 时按不歧义路径读。
    fetchLatestConstitution(rootId).then(applyConstitution).catch(() => undefined);
  }, [applyConstitution, rootId]);

  useEffect(() => {
    if (!constitutionDraft && tree?.constitution) {
      applyConstitution(tree.constitution);
    }
  }, [applyConstitution, constitutionDraft, tree?.constitution]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    fetchTask(selectedId)
      .then(setDetail)
      .catch(() => {
        setDetail(null);
      });
  }, [selectedId, tree]);

  const taskById = useMemo(() => {
    return new Map(tree?.tasks.map((task) => [task.id, task]) ?? []);
  }, [tree]);

  const parentIds = useMemo(() => {
    return new Set(tree?.edges.map((edge) => edge.source) ?? []);
  }, [tree]);

  const waitingReports = useMemo(() => buildWaitingReports(tree?.tasks ?? []), [tree]);

  const childrenByParent = useMemo(() => {
    const byParent = new Map<string, BattlefieldTask[]>();
    for (const task of tree?.tasks ?? []) {
      if (!task.parent_id) continue;
      const children = byParent.get(task.parent_id) ?? [];
      children.push(task);
      byParent.set(task.parent_id, children);
    }
    return byParent;
  }, [tree]);

  const descendantCounts = useMemo(() => {
    const countDescendants = (taskId: string): number => {
      const children = childrenByParent.get(taskId) ?? [];
      return children.length + children.reduce((sum, child) => sum + countDescendants(child.id), 0);
    };
    return new Map((tree?.tasks ?? []).map((task) => [task.id, countDescendants(task.id)]));
  }, [childrenByParent, tree]);

  const compactDoneArchiveActive = compactDoneArchive;

  const doneArchive = useMemo(() => {
    const hiddenIds = new Set<string>();
    const summaries = new Map<string, { nodeCount: number; rootCount: number }>();
    if (!tree) {
      return { hiddenIds, summaries };
    }
    for (const task of tree.tasks) {
      const archive = asRecord(task.node_meta.done_archive);
      if (!archive) continue;
      const nodeCount = Number(archive.node_count ?? 0);
      const rootCount = Number(archive.root_count ?? 0);
      if (nodeCount > 0) {
        summaries.set(task.id, { nodeCount, rootCount });
      }
    }
    if (summaries.size > 0 || statNumber(tree.stats, 'archived_done_nodes') > 0) {
      return { hiddenIds, summaries };
    }
    const subtreeDone = new Map<string, boolean>();
    const subtreeSize = new Map<string, number>();

    const isDoneSubtree = (task: BattlefieldTask): boolean => {
      const cached = subtreeDone.get(task.id);
      if (cached !== undefined) return cached;
      const children = childrenByParent.get(task.id) ?? [];
      const done = task.status === 'done' && children.every(isDoneSubtree);
      subtreeDone.set(task.id, done);
      return done;
    };

    const countSubtree = (task: BattlefieldTask): number => {
      const cached = subtreeSize.get(task.id);
      if (cached !== undefined) return cached;
      const children = childrenByParent.get(task.id) ?? [];
      const count = 1 + children.reduce((sum, child) => sum + countSubtree(child), 0);
      subtreeSize.set(task.id, count);
      return count;
    };

    const hideSubtree = (task: BattlefieldTask) => {
      hiddenIds.add(task.id);
      for (const child of childrenByParent.get(task.id) ?? []) {
        hideSubtree(child);
      }
    };

    for (const parent of tree.tasks) {
      const archivedRoots = (childrenByParent.get(parent.id) ?? []).filter(isDoneSubtree);
      if (archivedRoots.length === 0) continue;
      const nodeCount = archivedRoots.reduce((sum, child) => sum + countSubtree(child), 0);
      summaries.set(parent.id, { nodeCount, rootCount: archivedRoots.length });
      if (compactDoneArchiveActive && !expandedDoneArchiveParentIds.has(parent.id)) {
        for (const child of archivedRoots) {
          hideSubtree(child);
        }
      }
    }

    return { hiddenIds, summaries };
  }, [childrenByParent, compactDoneArchiveActive, expandedDoneArchiveParentIds, tree]);

  const availableDepths = useMemo(() => {
    return [...new Set(tree?.tasks.map((task) => task.depth) ?? [])].sort((a, b) => a - b);
  }, [tree]);

  const maxDepth = availableDepths.at(-1) ?? 0;
  const effectiveDepthLimit = visibleDepthLimit ?? maxDepth;
  const depthDisplayLabel =
    visibleDepthLimit === null ? `全部 ${maxDepth} 层` : visibleDepthLimit === 0 ? '仅根层' : `到第 ${visibleDepthLimit} 层`;

  const isHiddenByCollapse = useCallback(
    (task: BattlefieldTask) => {
      if (visibleDepthLimit !== null && task.depth > visibleDepthLimit) {
        return true;
      }
      let parentId = task.parent_id;
      while (parentId) {
        const parent = taskById.get(parentId);
        if (!parent) return false;
        if (collapsedNodeIds.has(parent.id)) {
          return true;
        }
        parentId = parent.parent_id;
      }
      return false;
    },
    [collapsedNodeIds, taskById, visibleDepthLimit],
  );

  const visibleTasks = useMemo(() => {
    if (!tree) return [];
    return tree.tasks.filter((task) => {
      if (doneArchive.hiddenIds.has(task.id)) return false;
      if (isHiddenByCollapse(task)) return false;
      if (statusFilter === 'all') return true;
      if (statusFilter === 'active') return task.is_active_frontier;
      return task.status === statusFilter;
    });
  }, [doneArchive.hiddenIds, isHiddenByCollapse, statusFilter, tree]);

  const toggleNodeCollapse = useCallback((task: BattlefieldTask) => {
    if (collapsedNodeIds.has(task.id)) {
      setCollapsedNodeIds((current) => {
        const next = new Set(current);
        next.delete(task.id);
        return next;
      });
      return;
    }
    if (visibleDepthLimit !== null && task.depth >= visibleDepthLimit && task.depth < maxDepth) {
      setVisibleDepthLimit(task.depth + 1);
      return;
    }
    setCollapsedNodeIds((current) => {
      const next = new Set(current);
      if (next.has(task.id)) {
        next.delete(task.id);
      } else {
        next.add(task.id);
      }
      return next;
    });
  }, [collapsedNodeIds, maxDepth, visibleDepthLimit]);

  const toggleDoneArchive = useCallback((task: BattlefieldTask) => {
    setExpandedDoneArchiveParentIds((current) => {
      const next = new Set(current);
      if (next.has(task.id)) {
        next.delete(task.id);
      } else {
        next.add(task.id);
      }
      return next;
    });
  }, []);

  const expandAll = useCallback(() => {
    setCollapsedNodeIds(new Set());
    setVisibleDepthLimit(null);
  }, []);

  const collapseOneLayer = useCallback(() => {
    setVisibleDepthLimit((current) => {
      const currentLimit = current ?? maxDepth;
      return Math.max(0, currentLimit - 1);
    });
  }, [maxDepth]);

  const expandOneLayer = useCallback(() => {
    setVisibleDepthLimit((current) => {
      const currentLimit = current ?? maxDepth;
      const nextLimit = Math.min(maxDepth, currentLimit + 1);
      return nextLimit >= maxDepth ? null : nextLimit;
    });
  }, [maxDepth]);

  const handleViewportMove = useCallback((_event: MouseEvent | TouchEvent | null, viewport: Viewport) => {
    const nextLod = nodeLodForZoom(viewport.zoom);
    setNodeLod((current) => (current === nextLod ? current : nextLod));
  }, []);

  const liveSpineEdgeIds = useMemo(() => {
    if (!tree) return new Set<string>();
    const allTasksById = new Map(tree.tasks.map((task) => [task.id, task]));
    const edgeIds = new Set<string>();
    tree.tasks
      .filter((task) => liveNodeStatuses.has(task.status))
      .forEach((task) => {
        let current: BattlefieldTask | undefined = task;
        while (current?.parent_id) {
          edgeIds.add(`${current.parent_id}-${current.id}`);
          current = allTasksById.get(current.parent_id);
        }
      });
    return edgeIds;
  }, [tree]);

  const edges = useMemo<Edge[]>(() => {
    if (!tree) return [];
    const visibleIds = new Set(visibleTasks.map((task) => task.id));
    const taskById = new Map(visibleTasks.map((task) => [task.id, task]));
    return tree.edges
      .filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target))
      .map((edge) => {
        const targetTask = taskById.get(edge.target);
        const edgeStyle = edgeStateStyles[targetTask?.status ?? 'planned'];
        const selectedPath = selectedId === edge.source || selectedId === edge.target ? 'selected-path' : '';
        const liveSpine = liveSpineEdgeIds.has(`${edge.source}-${edge.target}`);
        return {
          id: `${edge.source}-${edge.target}`,
          source: edge.source,
          target: edge.target,
          animated: edgeStyle.animated || liveSpine,
          type: 'step',
          className: `battle-edge ${edgeStyle.className} ${selectedPath} ${liveSpine ? 'live-spine' : ''}`,
          sourceHandle: 'bottom',
          targetHandle: 'top',
          interactionWidth: 18,
          style: {
            opacity: liveSpine || selectedPath ? 0.96 : 0.84,
            stroke: edgeStyle.color,
            strokeWidth: liveSpine ? 1.85 : selectedPath ? 2.15 : 1.45,
          },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            color: edgeStyle.color,
            width: liveSpine || selectedPath ? 11 : 9,
            height: liveSpine || selectedPath ? 11 : 9,
          },
          zIndex: liveSpine ? 14 : selectedPath ? 12 : targetTask?.is_active_frontier ? 8 : 5,
        };
      });
  }, [liveSpineEdgeIds, selectedId, tree, visibleTasks]);

  useEffect(() => {
    const nextNodes: Node[] = visibleTasks.map((task) => ({
      id: task.id,
      type: 'task',
      data: {
        task,
        selected: task.id === selectedId,
        isParent: parentIds.has(task.id),
        isCollapsed:
          collapsedNodeIds.has(task.id) ||
          (visibleDepthLimit !== null && task.depth >= visibleDepthLimit && (descendantCounts.get(task.id) ?? 0) > 0),
        hiddenDescendantCount: descendantCounts.get(task.id) ?? 0,
        archivedDoneCount: doneArchive.summaries.get(task.id)?.nodeCount ?? 0,
        archivedDoneRootCount: doneArchive.summaries.get(task.id)?.rootCount ?? 0,
        archivedDoneExpanded: expandedDoneArchiveParentIds.has(task.id) || !compactDoneArchiveActive,
        lod: nodeLod,
        waitingReport: waitingReports.get(task.id) ?? null,
        taskById,
        onToggleCollapse: toggleNodeCollapse,
        onToggleDoneArchive: toggleDoneArchive,
      } satisfies TaskNodeData,
      position: { x: 0, y: 0 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      width: 260,
      height: 148,
    }));
    layoutBattlefield(nextNodes, edges)
      .then(setNodes)
      .catch(() => setNodes(nextNodes));
  }, [
    collapsedNodeIds,
    compactDoneArchiveActive,
    descendantCounts,
    doneArchive.summaries,
    edges,
    expandedDoneArchiveParentIds,
    nodeLod,
    parentIds,
    selectedId,
    taskById,
    toggleDoneArchive,
    toggleNodeCollapse,
    visibleDepthLimit,
    visibleTasks,
    waitingReports,
  ]);

  const targetForRequest = targetPath.trim() || null;
  const activeConstitution = constitutionDraft ?? tree?.constitution ?? null;
  const treeConstitution = tree?.constitution ?? null;
  const treeHasTasks = (tree?.tasks.length ?? 0) > 0;
  const treeIsStale = !!activeConstitution && treeHasTasks && (!treeConstitution || activeConstitution.id !== treeConstitution.id);
  const visibleStats = treeIsStale ? null : (tree?.stats ?? null);
  const visibleSlotCapacity = statNumber(visibleStats, 'codex_slots_capacity', statNumber(visibleStats, 'max_concurrency', 1));
  const visibleActiveSlots = displaySlotCount(
    statNumber(visibleStats, 'codex_slots_active', statNumber(visibleStats, 'active_agents')),
    visibleSlotCapacity,
  );
  const visibleNodeCount = treeIsStale ? 0 : visibleTasks.length;
  const blockedSummary = useMemo(
    () => (treeIsStale ? null : summarizeBlockedNodes(tree?.tasks ?? [])),
    [tree, treeIsStale],
  );
  const activeRootId = tree?.tasks.find((task) => task.parent_id === null)?.id ?? null;
  const hasActiveFlow =
    !!tree &&
    (statNumber(tree.stats, 'active_agents') > 0 ||
      statNumber(tree.stats, 'queued') > 0 ||
      statNumber(tree.stats, 'planning') > 0 ||
      statNumber(tree.stats, 'working') > 0 ||
      statNumber(tree.stats, 'verifying') > 0 ||
      statNumber(tree.stats, 'accepting') > 0 ||
      statNumber(tree.stats, 'hibernating') > 0 ||
      statNumber(tree.stats, 'waiting_children') > 0);
  const constitutionMatchesInput =
    !!activeConstitution && activeConstitution.goal === goal && (activeConstitution.target_path ?? null) === targetForRequest;
  const constitutionLocked = activeConstitution?.status === 'locked';
  const canEditConstitution = !!activeConstitution && !constitutionLocked;

  const saveConstitutionDraft = async () => {
    if (!activeConstitution) {
      throw new Error('还没有项目宪法草案。');
    }
    if (activeConstitution.status === 'locked') {
      return activeConstitution;
    }
    const data = parseJsonObject(constitutionDataText);
    const saved = await updateConstitution(activeConstitution.id, constitutionMarkdown, data);
    applyConstitution(saved);
    return saved;
  };

  const onDraftConstitution = async () => {
    setBusy(true);
    setBusyLabel(runnerMode === 'real' ? '正在调用真实 Codex 生成宪法草案...' : '正在生成宪法草案...');
    setError(null);
    try {
      const draft = await draftConstitution(goal, runnerMode, targetForRequest);
      applyConstitution(draft);
      setSelectedId(null);
      setDetail(null);
      await loadTree();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onSaveConstitution = async () => {
    setBusy(true);
    setBusyLabel('正在保存宪法修订...');
    setError(null);
    try {
      await saveConstitutionDraft();
      await loadTree();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onLockConstitution = async () => {
    setBusy(true);
    setBusyLabel('正在锁定项目宪法...');
    setError(null);
    try {
      const saved = await saveConstitutionDraft();
      const locked = await lockConstitution(saved.id);
      applyConstitution(locked);
      await loadTree();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onStart = async () => {
    setBusy(true);
    setBusyLabel(runnerMode === 'real' ? '正在启动真实 Codex 任务树...' : '正在启动模拟任务树...');
    setError(null);
    let previewWindow: Window | null = null;
    try {
      if (!activeConstitution || activeConstitution.status !== 'locked') {
        throw new Error('请先锁定项目宪法。');
      }
      if (!constitutionMatchesInput) {
        throw new Error('当前根目标或目标路径与已锁定宪法不一致，请重新生成宪法草案。');
      }
      previewWindow = openPendingPreviewWindow();
      const response = await startProject(goal, runnerMode, targetForRequest, activeConstitution.id);
      const previewUrl = previewUrlForRoot(response.root_id);
      setRootId(response.root_id);
      writeRootIdToUrl(response.root_id);
      navigatePreviewWindow(previewWindow, previewUrl);
      previewWindow = null;
      setSelectedId(response.root_id);
      await loadTree(response.root_id);
    } catch (err) {
      previewWindow?.close();
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onOneShotStart = async () => {
    setBusy(true);
    setBusyLabel('正在用唯一输入启动递归任务树；执行模式由服务端配置决定...');
    setError(null);
    let previewWindow: Window | null = null;
    try {
      previewWindow = openPendingPreviewWindow();
      const response = await startOneShotProject(goal);
      const previewUrl = previewUrlForRoot(response.root_id);
      setRootId(response.root_id);
      writeRootIdToUrl(response.root_id);
      navigatePreviewWindow(previewWindow, previewUrl);
      previewWindow = null;
      setSelectedId(response.root_id);
      await loadTree(response.root_id);
      const latest = await fetchLatestConstitution(response.root_id);
      applyConstitution(latest);
    } catch (err) {
      previewWindow?.close();
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onAction = async (action: 'retry' | 'pause' | 'resume' | 'cancel') => {
    if (!selectedId) return;
    setBusy(true);
    setBusyLabel('正在更新节点状态...');
    setError(null);
    try {
      await postTaskAction(selectedId, action);
      await loadTree();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onCancelAll = async () => {
    setBusy(true);
    setBusyLabel('正在停止当前任务树...');
    setError(null);
    try {
      await cancelAllProjects(activeRootId);
      await loadTree();
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onHibernateRoot = async () => {
    if (!activeRootId) return;
    setBusy(true);
    setBusyLabel('正在启用预算守卫并低功耗休眠 5 小时...');
    setError(null);
    try {
      await setBudgetGuard(activeRootId, 18_000);
      await loadTree(activeRootId);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onInstallWatchdog = async () => {
    setBusy(true);
    setBusyLabel('正在安装/更新自动续跑钩子...');
    setError(null);
    try {
      const result = await installWatchdog(activeRootId);
      setWatchdogResult(
        result.status === 'succeeded'
          ? '安装/更新成功：定时巡检和 Startup 登录钩子已刷新；watchdog 本地巡检不消耗 Codex token。'
          : summarizeWatchdogAction(result),
      );
      await loadTree(activeRootId);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onRunWatchdogOnce = async () => {
    setBusy(true);
    setBusyLabel('正在手动巡检一次 watchdog...');
    setError(null);
    try {
      const result = await runWatchdogOnce(activeRootId);
      setWatchdogResult(
        result.status === 'succeeded'
          ? `巡检完成：未到恢复时间时只观察状态，不启动真实 Codex agent。${summarizeWatchdogAction(result)}`
          : summarizeWatchdogAction(result),
      );
      await loadTree(activeRootId);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  const onToggleApiRestartDrain = async () => {
    const shouldArm = !watchdogState?.api_restart_drain_armed;
    setBusy(true);
    setBusyLabel(shouldArm ? '正在武装维护排空...' : '正在取消维护排空...');
    setError(null);
    try {
      const result = await setApiRestartDrain(shouldArm);
      setWatchdogResult(
        result.api_restart_drain_armed
          ? `维护排空已武装：watchdog 会让任务树短暂低功耗休眠，等活跃 agent 归零后安全重启 API。${result.api_restart_drain_flag_path}`
          : '维护排空已取消：API 会继续等待自然空闲后再重启。',
      );
      await loadTree(activeRootId);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      setBusyLabel(null);
    }
  };

  return (
    <main className="shell">
      <aside className="left-rail">
        <div className="brand">
          <div className="brand-mark">
            <Zap size={22} />
          </div>
          <div>
            <h1>L4 Navigator</h1>
            <p>AI L4 自动导航任务可视化</p>
          </div>
        </div>

        <section className="panel">
          <label htmlFor="goal">导航目标</label>
          <textarea id="goal" value={goal} onChange={(event) => setGoal(event.target.value)} />
          <label htmlFor="targetPath">目标路径（高级模式）</label>
          <input
            id="targetPath"
            value={targetPath}
            onChange={(event) => setTargetPath(event.target.value)}
            placeholder="可选：Git 仓库或工作区路径"
          />
          <div className="mode-toggle" role="group" aria-label="运行模式">
            <button className={runnerMode === 'mock' ? 'active' : ''} onClick={() => setRunnerMode('mock')} type="button">
              模拟
            </button>
            <button className={runnerMode === 'real' ? 'active' : ''} onClick={() => setRunnerMode('real')} type="button">
              真实 Codex
            </button>
          </div>
          <p className="input-hint">L4 入口只提交目标文本；系统会自动生成边界、拆解路径、调度 Codex agent，并把执行进度映射到画布。</p>
        </section>

        <section className="panel constitution-editor" aria-label="项目宪法编辑">
          <div className="panel-row">
            <label htmlFor="constitutionMarkdown">项目宪法</label>
            <span className={`constitution-chip ${activeConstitution?.status ?? 'empty'}`}>
              {constitutionLocked ? '已锁定' : activeConstitution ? '草案' : '未生成'}
            </span>
          </div>
          <div className="constitution-actions">
            <button className="secondary" onClick={onDraftConstitution} disabled={busy || goal.trim().length === 0} type="button" title="生成宪法草案">
              <FileText size={15} />
              生成草案
            </button>
            <button className="secondary" onClick={onSaveConstitution} disabled={busy || !canEditConstitution} type="button" title="保存宪法修订">
              <RefreshCw size={15} />
              保存修订
            </button>
          </div>
          {activeConstitution ? (
            <>
              <textarea
                id="constitutionMarkdown"
                className="constitution-markdown"
                value={constitutionMarkdown}
                onChange={(event) => setConstitutionMarkdown(event.target.value)}
                disabled={!canEditConstitution}
              />
              <label htmlFor="constitutionData">结构化边界</label>
              <textarea
                id="constitutionData"
                className="constitution-data"
                value={constitutionDataText}
                onChange={(event) => setConstitutionDataText(event.target.value)}
                disabled={!canEditConstitution}
              />
              <div className="constitution-actions">
                <button className="secondary" onClick={onLockConstitution} disabled={busy || !canEditConstitution} type="button" title="锁定项目宪法">
                  <ShieldCheck size={15} />
                  锁定宪法
                </button>
                <button
                  className="primary"
                  onClick={onStart}
                  disabled={busy || !constitutionLocked || !constitutionMatchesInput}
                  type="button"
                  title="启动递归任务树"
                >
                  <Play size={17} />
                  启动任务树
                </button>
              </div>
              <code className="constitution-hash">
                v{activeConstitution.version} · {activeConstitution.content_hash}
              </code>
            </>
          ) : (
            <div className="constitution-empty">等待草案</div>
          )}
        </section>

        <section className="panel">
          <label htmlFor="filter">视图</label>
          <div className="select-wrap">
            <Search size={16} />
            <select id="filter" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as TaskStatus | 'all' | 'active')}>
              <option value="all">全部节点</option>
              <option value="active">当前前线</option>
              {Object.entries(statusLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div className="archive-toggle-row">
            <button
              className="active"
              onClick={() => setExpandedDoneArchiveParentIds(new Set())}
              type="button"
              title="完成历史按父节点局部展开；点击这里收回已经展开的历史分支，避免长跑画布卡顿。"
            >
              <Archive size={15} />
              轻量历史保护
            </button>
            <span>
              已压缩 {statNumber(tree?.stats, 'archived_done_nodes', doneArchive.hiddenIds.size)} 个完成节点；点节点上的归档数字局部展开
            </span>
          </div>
        </section>

        <section className="panel">
          <div className="panel-row">
            <label>显示深度</label>
            <button className="ghost-action" onClick={expandAll} type="button" title="展开全部">
              <Maximize2 size={15} />
            </button>
          </div>
          <div className="depth-stepper" aria-label="逐层展开折叠">
            <button onClick={collapseOneLayer} disabled={treeIsStale || effectiveDepthLimit <= 0} type="button" title="向上收起一层">
              <ChevronsUp size={16} />
            </button>
            <span>{depthDisplayLabel}</span>
            <button onClick={expandOneLayer} disabled={treeIsStale || visibleDepthLimit === null || effectiveDepthLimit >= maxDepth} type="button" title="向下展开一层">
              <ChevronsDown size={16} />
            </button>
          </div>
          <div className="depth-controls" aria-label="自上而下显示深度控制">
            {availableDepths.map((depth) => (
              <button
                key={depth}
                className={visibleDepthLimit === depth ? 'active' : ''}
                onClick={() => setVisibleDepthLimit(depth)}
                type="button"
                title={depth === 0 ? '只显示根层' : `显示到第 ${depth} 层`}
              >
                <Layers size={14} />
                {depth === 0 ? '根层' : `到 ${depth} 层`}
              </button>
            ))}
            <button className={visibleDepthLimit === null ? 'active' : ''} onClick={expandAll} type="button" title="显示全部层级">
              <Maximize2 size={14} />
              全部
            </button>
          </div>
        </section>

        <section className="stat-grid" aria-label="战场统计主项">
          <Stat label="总数" value={statNumber(visibleStats, 'total')} />
          <Stat label="完成" value={statNumber(visibleStats, 'done')} />
          <Stat
            label="Codex 槽位"
            value={`${visibleActiveSlots}/${visibleSlotCapacity}`}
            title="真实正在占用 Codex CLI 的并发槽位。"
          />
          <Stat label="运行节点" value={statNumber(visibleStats, 'active_agent_statuses', statNumber(visibleStats, 'active_agents'))} title="数据库中处于执行/验收/整合状态的节点；真实消耗以 Codex 槽位为准。" />
          <Stat label="并发上限" value={statNumber(visibleStats, 'max_concurrency', 1)} />
          <Stat
            label="自动油门"
            value={`${autoConcurrencyLabel(statText(visibleStats, 'auto_concurrency_mode', 'pending'))} ${statNumber(
              visibleStats,
              'auto_concurrency_min',
              1,
            )}-${statNumber(visibleStats, 'auto_concurrency_max', 1)}`}
            title={statText(visibleStats, 'auto_concurrency_reason', '等待下一次调度评估。')}
          />
        </section>

        <details className="stat-more">
          <summary>更多指标（模型、预算、状态细分）</summary>
          <section className="stat-grid" aria-label="战场统计其余项">
            <Stat label="Codex 模型" value={statText(visibleStats, 'codex_model')} />
            <Stat label="推理档位" value={statText(visibleStats, 'codex_reasoning_effort')} />
            <Stat label="Goal 模式" value={statNumber(visibleStats, 'goal_mode') ? '长跑' : '保守'} />
            <Stat label="重试次数" value={statNumber(visibleStats, 'codex_retry_attempts')} />
            <Stat
              label="启动预算"
              value={
                statNumber(visibleStats, 'agent_burst_limit') > 0
                  ? `${statNumber(visibleStats, 'agent_burst_used')}/${statNumber(
                      visibleStats,
                      'agent_burst_work_limit',
                      statNumber(visibleStats, 'agent_burst_limit'),
                    )}`
                  : '不限'
              }
            />
            <Stat label="自动保留" value={statNumber(visibleStats, 'agent_burst_reserve')} />
            <Stat label="剩余工作" value={statNumber(visibleStats, 'agent_burst_remaining')} />
            <Stat label="预算自启" value={`${Math.round(statNumber(visibleStats, 'agent_burst_resume_after_seconds') / 3600)} 小时`} />
            <Stat label="硬超时" value={statNumber(visibleStats, 'codex_timeout_seconds')} />
            <Stat label="空闲看门狗" value={statNumber(visibleStats, 'codex_idle_timeout_seconds')} />
            <Stat label="Godot 超时" value={statNumber(visibleStats, 'godot_timeout_seconds')} />
            <Stat label="自动休眠" value={statNumber(visibleStats, 'auto_hibernate_on_resource_block') ? '开启' : '关闭'} />
            <Stat label="自启排程" value={statNumber(visibleStats, 'scheduled_auto_resumes')} />
            <Stat label="接力延迟" value={`${statNumber(visibleStats, 'scheduler_deferred_retry_seconds')} 秒`} />
            <Stat label="待接力" value={statNumber(visibleStats, 'scheduled_deferred_resumes')} />
            <Stat label="排队中" value={statNumber(visibleStats, 'queued')} />
            <Stat label="等待父节点" value={statNumber(visibleStats, 'waiting_children')} />
            <Stat label="执行中" value={statNumber(visibleStats, 'working', statNumber(visibleStats, 'running'))} />
            <Stat label="验收中" value={statNumber(visibleStats, 'verifying')} />
            <Stat label="休眠中" value={statNumber(visibleStats, 'hibernating')} />
            <Stat label="阻塞" value={statNumber(visibleStats, 'blocked')} />
            <Stat label="有效阻塞" value={statNumber(visibleStats, 'blocked_active')} />
            <Stat label="额度等待" value={statNumber(visibleStats, 'blocked_resource_wait')} />
            <Stat label="修复中阻塞" value={statNumber(visibleStats, 'blocked_under_repair')} />
            <Stat label="停用分支阻塞" value={statNumber(visibleStats, 'blocked_inactive_branch')} />
            <Stat label="前线" value={statNumber(visibleStats, 'active_frontier')} />
          </section>
        </details>

        <WatchdogPanel
          state={watchdogState}
          layout={layoutState}
          busy={busy}
          result={watchdogResult}
          onInstall={onInstallWatchdog}
          onRunOnce={onRunWatchdogOnce}
          onToggleDrain={onToggleApiRestartDrain}
        />

        <StewardPanel layout={layoutState} onOpen={() => loadTree().catch(() => undefined)} />

        <ConstitutionPanel
          constitution={activeConstitution}
          board={constitutionBoardFromStats(treeIsStale ? undefined : tree?.stats)}
        />

        <ObligationBoardPanel board={obligationBoardFromStats(treeIsStale ? undefined : tree?.stats)} />

        <ObligationIndexPanel board={obligationIndex} />

        <EventTicker events={treeIsStale ? [] : (tree?.events ?? [])} />

        {error && <div className="error">{error}</div>}
      </aside>

      <section className="battlefield" aria-label="L4 自动导航画布">
        {treeIsStale ? (
          <div className="tree-empty-state">
            <ShieldCheck size={28} />
            <h3>导航边界已生成，等待启航</h3>
            <p>当前画布已隐藏上一条旧航线。请先锁定这份边界，再启动导航；启动后这里会显示递归拆解与完成路径。</p>
            <code>
              当前宪法 {activeConstitution?.content_hash ?? '未生成'} · 旧树宪法 {treeConstitution?.content_hash ?? '无'}
            </code>
          </div>
        ) : (
          <ReactFlow
            className={`tree-flow lod-${nodeLod}`}
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            minZoom={0.15}
            maxZoom={1.5}
            elevateEdgesOnSelect
            onInit={(instance) => setNodeLod(nodeLodForZoom(instance.getZoom()))}
            onMove={handleViewportMove}
            onNodeClick={(_, node) => setSelectedId(node.id)}
            onNodeDoubleClick={(_, node) => {
              const task = (node.data as TaskNodeData).task;
              if (parentIds.has(task.id)) {
                toggleNodeCollapse(task);
              }
            }}
          >
            <Background gap={24} size={1} />
            <MiniMap pannable zoomable nodeStrokeWidth={3} />
            <Controls />
          </ReactFlow>
        )}
        <GlobalNavigationDock
          stats={visibleStats}
          usage={treeIsStale ? null : (tree?.usage ?? null)}
          blocked={blockedSummary}
          visibleNodeCount={visibleNodeCount}
          maxDepth={statNumber(visibleStats, 'max_depth')}
          busy={busy}
          busyLabel={busyLabel}
          canStart={goal.trim().length > 0}
          canControl={!!tree?.tasks.length && hasActiveFlow}
          canHibernate={!!activeRootId && hasActiveFlow}
          treeIsStale={treeIsStale}
          effectiveDepthLimit={effectiveDepthLimit}
          visibleDepthLimit={visibleDepthLimit}
          maxDepthValue={maxDepth}
          onStart={onOneShotStart}
          onHibernate={onHibernateRoot}
          onCancel={onCancelAll}
          onCollapseOneLayer={collapseOneLayer}
          onExpandOneLayer={expandOneLayer}
          onExpandAll={expandAll}
        />
      </section>

      <aside className="right-rail">
        <DetailPanel
          detail={treeIsStale ? null : detail}
          waitingReport={detail?.task.id ? (waitingReports.get(detail.task.id) ?? null) : null}
          taskById={taskById}
          busy={busy}
          busyLabel={busyLabel}
          onAction={onAction}
        />
      </aside>
    </main>
  );
}

function formatTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

function formatUsageSummary(usage: TreeUsage | null | undefined): string | null {
  if (!usage) return null;
  const input = usage.input_tokens ?? 0;
  const output = usage.output_tokens ?? 0;
  const cached = usage.cached_input_tokens ?? 0;
  if (!input && !output && !(usage.total_tokens ?? 0)) return null;
  const parts = [`入 ${formatTokenCount(input)}`, `出 ${formatTokenCount(output)}`];
  if (input > 0 && cached > 0) parts.push(`缓存 ${Math.round((cached / input) * 100)}%`);
  if (usage.runs) parts.push(`${usage.runs} 次调用`);
  return `用量 ${parts.join(' · ')}`;
}

function formatCostSummary(usage: TreeUsage | null | undefined): string | null {
  const buckets = usage?.cost_by_currency;
  if (!buckets) return null;
  const entries = Object.entries(buckets).filter(([, amount]) => typeof amount === 'number' && amount > 0);
  if (!entries.length) return null;
  const symbols: Record<string, string> = { CNY: '¥', USD: '$' };
  // 不同币种分别显示，不相加——与后端 summarize_usages 的口径一致。
  return entries.map(([code, amount]) => `${symbols[code] ?? `${code} `}${amount.toFixed(4)}`).join(' / ');
}

function EventTicker({ events }: { events: BattlefieldEvent[] }) {
  if (!events.length) {
    return null;
  }
  // 后端按时间正序返回；这里倒序取最近若干条，让失败与阻塞第一时间可见。
  const recent = [...events].reverse().slice(0, 8);
  return (
    <section className="panel event-ticker">
      <div className="event-ticker-head">
        <Clock size={14} />
        <span>最近事件</span>
        <span className="event-ticker-count">{events.length}</span>
      </div>
      <ul>
        {recent.map((event) => (
          <li key={event.id} className={eventSeverity(event.type)} title={`${event.type}\n${event.message}`}>
            <span className="event-ticker-time">{formatEventClock(event.created_at)}</span>
            <span className="event-ticker-label">{eventTypeLabels[event.type] ?? event.type}</span>
            <span className="event-ticker-message">{event.message}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function channelLabel(raw: string): { label: string; detail: string } {
  const value = (raw ?? '').trim();
  if (!value || value === 'default') {
    return { label: '默认通道', detail: '使用默认 ~/.codex 配置' };
  }
  const parts = value.split(/[\\/]+/).filter(Boolean);
  return { label: parts.length ? parts[parts.length - 1] : value, detail: value };
}

function GlobalNavigationDock({
  stats,
  usage,
  blocked,
  visibleNodeCount,
  maxDepth,
  busy,
  busyLabel,
  canStart,
  canControl,
  canHibernate,
  treeIsStale,
  effectiveDepthLimit,
  visibleDepthLimit,
  maxDepthValue,
  onStart,
  onHibernate,
  onCancel,
  onCollapseOneLayer,
  onExpandOneLayer,
  onExpandAll,
}: {
  stats: TreeStats | null | undefined;
  usage?: TreeUsage | null;
  blocked?: { count: number; top: string | null; topTaskId: string | null; topTaskTitle: string | null } | null;
  visibleNodeCount: number;
  maxDepth: number;
  busy: boolean;
  busyLabel: string | null;
  canStart: boolean;
  canControl: boolean;
  canHibernate: boolean;
  treeIsStale: boolean;
  effectiveDepthLimit: number;
  visibleDepthLimit: number | null;
  maxDepthValue: number;
  onStart: () => void | Promise<void>;
  onHibernate: () => void | Promise<void>;
  onCancel: () => void | Promise<void>;
  onCollapseOneLayer: () => void;
  onExpandOneLayer: () => void;
  onExpandAll: () => void;
}) {
  const total = statNumber(stats, 'total');
  const done = statNumber(stats, 'done');
  const queued = statNumber(stats, 'queued');
  const waiting = statNumber(stats, 'waiting_children');
  const working = statNumber(stats, 'working', statNumber(stats, 'running'));
  const verifying = statNumber(stats, 'verifying');
  const accepting = statNumber(stats, 'accepting');
  const hibernating = statNumber(stats, 'hibernating');
  const rawBlocked = statNumber(stats, 'blocked');
  const activeBlocked = statNumber(stats, 'blocked_active', rawBlocked);
  const failed = statNumber(stats, 'failed');
  const activeNodeStatuses = statNumber(stats, 'active_agent_statuses', statNumber(stats, 'active_agents'));
  const slotCapacity = statNumber(stats, 'codex_slots_capacity', statNumber(stats, 'max_concurrency', 1));
  const activeSlots = displaySlotCount(statNumber(stats, 'codex_slots_active', activeNodeStatuses), slotCapacity);
  const activeFrontier = statNumber(stats, 'active_frontier');
  const activeWork = working + verifying + accepting + activeSlots;
  const usageLine = formatUsageSummary(usage);
  const costLine = formatCostSummary(usage);
  const modelLabel = statText(stats, 'codex_model', '');
  const channel = channelLabel(statText(stats, 'codex_home', 'default'));

  let tone = 'idle';
  let label = '待启动';
  if (busy) {
    tone = 'busy';
    label = '指令处理中';
  } else if (activeBlocked > 0 || failed > 0) {
    tone = 'attention';
    label = '需要接管';
  } else if (hibernating > 0) {
    tone = 'sleeping';
    label = '低功耗休眠';
  } else if (activeWork > 0) {
    tone = 'running';
    label = 'L4 自动导航中';
  } else if (queued > 0 || waiting > 0) {
    tone = 'queued';
    label = '等待接力';
  } else if (total > 0 && done >= total) {
    tone = 'done';
    label = '已抵达';
  }

  const depthText = visibleDepthLimit === null ? `全局 ${maxDepth} 层` : `显示到第 ${effectiveDepthLimit} 层`;
  const detail = `完成 ${done}/${total} · 槽位 ${activeSlots}/${slotCapacity} · 运行节点 ${activeNodeStatuses} · 前线 ${activeFrontier} · 显示 ${visibleNodeCount} · ${depthText}`;

  return (
    <div className="navigation-dock" aria-label="L4 自动导航控制台">
      <div className={`navigation-state ${tone}`}>
        <span className="state-dot" aria-hidden="true" />
        <div>
          <strong>{label}</strong>
          <span>{detail}</span>
        </div>
      </div>

      <div className="dock-actions" aria-label="整棵树控制">
        <button className="dock-button primary" onClick={onStart} disabled={busy || !canStart || canControl} type="button" title="启动一棵新的 L4 任务树">
          <Play size={16} />
          L4 启航
        </button>
        <button className="dock-button" onClick={onHibernate} disabled={busy || !canHibernate} type="button" title="保存状态并进入低功耗等待">
          <Clock size={16} />
          低功耗
        </button>
        <button className="dock-button danger" onClick={onCancel} disabled={busy || !canControl} type="button" title="停止当前任务树">
          <Ban size={16} />
          停止
        </button>
      </div>

      <span className="dock-divider" aria-hidden="true" />

      <div className="dock-view-actions" aria-label="任务树视图控制">
        <button className="dock-icon-button" onClick={onCollapseOneLayer} disabled={treeIsStale || effectiveDepthLimit <= 0} type="button" title="向上收起一层">
          <ChevronsUp size={16} />
        </button>
        <button
          className="dock-icon-button"
          onClick={onExpandOneLayer}
          disabled={treeIsStale || visibleDepthLimit === null || effectiveDepthLimit >= maxDepthValue}
          type="button"
          title="向下展开一层"
        >
          <ChevronsDown size={16} />
        </button>
        <button className="dock-icon-button" onClick={onExpandAll} disabled={treeIsStale} type="button" title="展开全局">
          <Maximize2 size={16} />
        </button>
      </div>

      <div className="dock-legend" aria-label="状态图例">
        <span className="legend-chip running">执行中</span>
        <span className="legend-chip verifying">验收</span>
        <span className="legend-chip done">完成</span>
        <span className="legend-chip blocked">阻塞</span>
      </div>

      {modelLabel && (
        <div className="dock-model" title={`模型 ${modelLabel}\n配置目录 ${channel.detail}`}>
          <Terminal size={13} />
          <span>{modelLabel}</span>
          <span className="dock-model-sep">·</span>
          <span>{channel.label}</span>
        </div>
      )}

      {blocked && blocked.count > 0 && (
        <div
          className="dock-blocked"
          title={
            blocked.top
              ? `阻塞 ${blocked.count} 个节点。首要：「${blocked.topTaskTitle ?? blocked.topTaskId}」\n${blocked.top}`
              : `阻塞 ${blocked.count} 个节点，但尚未记录可读的阻塞原因；请在节点详情中查看运行记录。`
          }
        >
          <Ban size={13} />
          <span className="dock-blocked-count">阻塞 {blocked.count}</span>
          {blocked.top && (
            <span className="dock-blocked-reason">
              {blocked.top.length > 46 ? `${blocked.top.slice(0, 46)}…` : blocked.top}
            </span>
          )}
        </div>
      )}

      {usageLine && (
        <div
          className="dock-usage"
          title={
            costLine
              ? '累计 token 用量与供应商回报的金额（按币种分别显示，不相加）。'
              : '累计 token 用量（按输入/输出计）。当前供应商未在运行结果里回报金额，因此只显示用量。'
          }
        >
          <span>{usageLine}</span>
          {costLine && <span className="dock-usage-cost">{costLine}</span>}
        </div>
      )}

      {busyLabel && (
        <div className="dock-busy">
          <BusyRunner label={busyLabel} compact />
        </div>
      )}
    </div>
  );
}

function TaskNodeView(props: NodeProps) {
  const {
    task,
    selected,
    isParent,
    isCollapsed,
    hiddenDescendantCount,
    archivedDoneCount,
    archivedDoneRootCount,
    archivedDoneExpanded,
    lod,
    waitingReport: fallbackWaitingReport,
    taskById,
    onToggleCollapse,
    onToggleDoneArchive,
  } = props.data as TaskNodeData;
  const isLive = liveNodeStatuses.has(task.status);
  const waitingReport = asRecord(task.node_meta.waiting_report) ?? fallbackWaitingReport;
  const isReportingParent = task.status === 'waiting_children' && !!waitingReport;
  const isQueued = task.status === 'queued';
  const reportDone = numberValue(waitingReport?.direct_done);
  const reportTotal = numberValue(waitingReport?.direct_total);
  const blockSummary = nodeBlockSummary(task, taskById);
  const childBlockCount = asRecordArray(task.node_meta.blocked_child_reports).length;
  const outcomePreview = lod === 'full' ? outcomePreviewForTask(task) : null;
  const evidenceBadges = taskEvidenceBadges(task);
  return (
    <div
      className={`task-node lod-${lod} ${task.status} ${isLive ? 'live-node' : ''} ${isReportingParent ? 'reporting-parent' : ''} ${isQueued ? 'queued-waiting' : ''} ${blockSummary ? 'has-block-report' : ''} ${outcomePreview ? 'has-outcome-preview' : ''} ${task.is_active_frontier ? 'frontier' : ''} ${selected ? 'selected' : ''}`}
    >
      <Handle id="top" type="target" position={Position.Top} />
      {isLive && <span className="node-flow" aria-hidden="true" />}
      {isQueued && <span className="queue-flow" aria-hidden="true" />}
      {outcomePreview && (
        <div className={`outcome-preview ${outcomePreview.kind}`} title={outcomePreview.title} aria-label={outcomePreview.label}>
          {outcomePreview.kind === 'image' ? (
            <img src={outcomePreview.imageUrl} alt="" loading="lazy" />
          ) : (
            <FileText size={20} aria-hidden="true" />
          )}
          <span>{outcomePreview.label}</span>
        </div>
      )}
      <div className="node-row">
        <div className="node-meta">
          {lod === 'full' && <span className="depth">第 {task.depth} 层</span>}
          {isParent && (
            <button
              className={`collapse-node ${isCollapsed ? 'collapsed' : ''}`}
              onClick={(event) => {
                event.stopPropagation();
                onToggleCollapse(task);
              }}
              aria-label={isCollapsed ? `展开 ${hiddenDescendantCount} 个子节点` : `收起 ${hiddenDescendantCount} 个子节点`}
              title={isCollapsed ? `展开 ${hiddenDescendantCount} 个子节点` : `收起 ${hiddenDescendantCount} 个子节点`}
              type="button"
            >
              {isCollapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
              <span>{isCollapsed ? '展开' : '收起'}</span>
              <b>{hiddenDescendantCount}</b>
            </button>
          )}
        </div>
        <span className={`status ${task.status}`}>{statusLabels[task.status]}</span>
      </div>
      <strong>{task.title}</strong>
      <div className="node-badges" aria-label="节点递归标记">
        {isReportingParent && <span className="report-badge">汇报 {reportDone}/{reportTotal}</span>}
        {isQueued && <span>等待槽位</span>}
        {archivedDoneCount > 0 && (
          <button
            className={`done-archive-chip ${archivedDoneExpanded ? 'expanded' : ''}`}
            onClick={(event) => {
              event.stopPropagation();
              onToggleDoneArchive(task);
            }}
            title={
              archivedDoneExpanded
                ? `收回 ${archivedDoneCount} 个已完成历史节点`
                : `展开 ${archivedDoneRootCount} 条已完成分支，共 ${archivedDoneCount} 个历史节点`
            }
            type="button"
          >
            <Archive size={12} />
            <span>{archivedDoneExpanded ? '收回历史' : '完成归档'}</span>
            <b>{archivedDoneCount}</b>
          </button>
        )}
        {blockSummary && <span className="block-badge">{childBlockCount > 0 ? `子阻塞 ${childBlockCount}` : '阻塞上报'}</span>}
        {task.converted_from_leaf && <span>叶子转父</span>}
        {task.verification_failures > 0 && <span>失败 {task.verification_failures}</span>}
        {task.leaf_attempts > 0 && <span>叶子尝试 {task.leaf_attempts}</span>}
        {evidenceBadges.evidence > 0 && (
          <span className="evidence-badge" title="worker 提交的证据条数（含父层整合验收的证据）">
            证据 {evidenceBadges.evidence}
          </span>
        )}
        {evidenceBadges.changed > 0 && (
          <span className="evidence-badge" title="worker 声明的变更文件数">
            变更 {evidenceBadges.changed}
          </span>
        )}
        {evidenceBadges.tests > 0 && (
          <span className="evidence-badge" title="worker 提交的验证命令 / 测试条数">
            验证 {evidenceBadges.tests}
          </span>
        )}
      </div>
      {blockSummary && (
        <div className="node-report-line" title={blockSummary}>
          <span>{childBlockCount > 0 ? '子任务' : '阻塞'}</span>
          <strong>{blockSummary}</strong>
        </div>
      )}
      <p>{task.description || '暂无描述。'}</p>
      <Handle id="bottom" type="source" position={Position.Bottom} />
    </div>
  );
}

function DetailPanel({
  detail,
  waitingReport: fallbackWaitingReport,
  taskById,
  busy,
  busyLabel,
  onAction,
}: {
  detail: TaskDetailResponse | null;
  waitingReport: Record<string, unknown> | null;
  taskById: Map<string, BattlefieldTask>;
  busy: boolean;
  busyLabel: string | null;
  onAction: (action: 'retry' | 'pause' | 'resume' | 'cancel') => void;
}) {
  const [artifact, setArtifact] = useState<RunArtifactResponse | null>(null);
  const [artifactKey, setArtifactKey] = useState<string | null>(null);
  const [artifactLoading, setArtifactLoading] = useState(false);
  const [artifactError, setArtifactError] = useState<string | null>(null);
  const usageLine = formatUsageSummary(detail?.usage);
  const costLine = formatCostSummary(detail?.usage);

  useEffect(() => {
    setArtifact(null);
    setArtifactKey(null);
    setArtifactError(null);
    setArtifactLoading(false);
  }, [detail?.task.id]);

  const loadArtifact = async (runId: number, kind: ArtifactKind) => {
    const key = `${runId}:${kind}`;
    setArtifactKey(key);
    setArtifactLoading(true);
    setArtifactError(null);
    try {
      setArtifact(await fetchRunArtifact(runId, kind));
    } catch (err) {
      setArtifact(null);
      setArtifactError(String(err));
    } finally {
      setArtifactLoading(false);
    }
  };

  if (!detail) {
    return (
      <div className="detail-empty">
        <Activity size={28} />
        <p>选择一个节点，查看它的证据、运行记录和验收状态。</p>
      </div>
    );
  }

  const { task } = detail;
  const blockReport = asRecord(task.node_meta.block_report);
  const childBlockReports = asRecordArray(task.node_meta.blocked_child_reports);
  const waitingReport = asRecord(task.node_meta.waiting_report) ?? fallbackWaitingReport;
  const reportActiveLeaves = asRecordArray(waitingReport?.active_leaves);
  const reportBlockedNodes = asRecordArray(waitingReport?.blocked_nodes);
  const tracedBlockSummary = nodeBlockSummary(task, taskById);
  return (
    <div className="detail">
      <div className="detail-head">
        <span className={`status ${task.status}`}>{statusLabels[task.status]}</span>
        {task.is_active_frontier && <span className="frontier-pill">当前前线</span>}
      </div>
      <h2>{task.title}</h2>
      <p className="muted">{task.description}</p>

      <div className="actions">
        <button onClick={() => onAction('retry')} disabled={busy} title="重试此任务">
          <RotateCcw size={16} />
        </button>
        <button onClick={() => onAction('pause')} disabled={busy} title="暂停此任务">
          <Pause size={16} />
        </button>
        <button onClick={() => onAction('resume')} disabled={busy} title="继续此任务">
          <RefreshCw size={16} />
        </button>
        <button onClick={() => onAction('cancel')} disabled={busy} title="取消此任务">
          <Ban size={16} />
        </button>
      </div>
      {busyLabel && <BusyRunner label={busyLabel} compact />}

      <section className="detail-section">
        <h3>递归判断</h3>
        <div className="contract-grid">
          <span>宪法版本</span>
          <strong>{task.constitution_hash ?? '未绑定'}</strong>
          <span>叶子尝试</span>
          <strong>{task.leaf_attempts}</strong>
          <span>验收失败</span>
          <strong>{task.verification_failures}</strong>
          <span>叶子转父</span>
          <strong>{task.converted_from_leaf ? '是' : '否'}</strong>
        </div>
        {Object.keys(task.node_meta).length > 0 && <code className="json-block">{JSON.stringify(task.node_meta, null, 2)}</code>}
      </section>

      {waitingReport && (
        <section className="detail-section waiting-report">
          <h3>父节点汇报</h3>
          <p>{String(waitingReport.summary ?? '等待子任务收敛。')}</p>
          <div className="report-grid">
            <span>直属完成</span>
            <strong>
              {numberValue(waitingReport.direct_done)}/{numberValue(waitingReport.direct_total)}
            </strong>
            <span>子树完成</span>
            <strong>
              {numberValue(waitingReport.subtree_done)}/{numberValue(waitingReport.subtree_total)}
            </strong>
            <span>真实运行叶子</span>
            <strong>{numberValue(waitingReport.active_agents)}</strong>
            <span>排队等待</span>
            <strong>{numberValue(waitingReport.queued_descendants)}</strong>
            <span>阻塞/修复</span>
            <strong>{numberValue(waitingReport.blocked_descendants)}</strong>
          </div>
          {reportActiveLeaves.length > 0 && (
            <div className="report-list">
              <strong>当前真正运行的叶子</strong>
              {reportActiveLeaves.map((leaf) => (
                <span key={String(leaf.id)}>
                  {String(leaf.title ?? leaf.id)} · {statusLabels[String(leaf.status) as TaskStatus] ?? String(leaf.status ?? '')}
                </span>
              ))}
            </div>
          )}
          {reportBlockedNodes.length > 0 && (
            <div className="report-list blocked">
              <strong>需要父层处理的节点</strong>
              {reportBlockedNodes.map((node) => (
                <span key={String(node.id)}>
                  {String(node.title ?? node.id)} · {String(node.reason ?? statusLabels[String(node.status) as TaskStatus] ?? '')}
                </span>
              ))}
            </div>
          )}
        </section>
      )}

      {(task.error_message || blockReport || childBlockReports.length > 0) && (
        <section className="detail-section block-report">
          <h3>阻塞上报</h3>
          {tracedBlockSummary && <p className="block-root-cause">追溯根因：{tracedBlockSummary}</p>}
          {task.error_message && <p>{task.error_message}</p>}
          {blockReport && (
            <div className="block-report-grid">
              <span>来源</span>
              <strong>{String(blockReport.source ?? '未知')}</strong>
              <span>分类</span>
              <strong>{String(blockReport.category ?? 'external')}</strong>
              <span>可重试</span>
              <strong>{blockRetryableLabel(blockReport.retryable, blockReport.parent_action)}</strong>
              <span>父层动作</span>
              <strong>{blockParentActionLabel(blockReport.parent_action)}</strong>
            </div>
          )}
          {Boolean(blockReport?.skill_lookup) && typeof blockReport?.skill_lookup === 'object' && (
            <div className="skill-lookup-box">
              <strong>Skill/KM 自救</strong>
              <span>{String((blockReport.skill_lookup as Record<string, unknown>).recommended ?? false) === 'true' ? '建议先检索解决方案' : '已记录知识库入口'}</span>
              <code>{String((blockReport.skill_lookup as Record<string, unknown>).url ?? '未配置')}</code>
              {Boolean(blockReport.human_action_required) && <p>当前阻塞需要人工登录、授权或权限处理；父层可继续推进其他不依赖它的任务。</p>}
            </div>
          )}
          {childBlockReports.length > 0 && (
            <div className="child-report-list">
              {childBlockReports.map((report, index) => (
                <div className="child-report" key={`${String(report.task_id ?? index)}-${index}`}>
                  <strong>{String(report.title ?? report.task_id ?? '子节点')}</strong>
                  <span>{blockParentActionLabel(report.retryable ? 'wait_and_retry' : report.parent_action)}</span>
                  <p>{String(report.reason ?? '暂无原因')}</p>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      <Section title="验收标准" items={task.acceptance} empty="暂无验收标准。" />
      <Section title="任务边界" items={task.scope} empty="暂无明确边界。" />

      <section className="detail-section">
        <h3>
          <Activity size={16} />
          用量与成本
        </h3>
        <div className="usage-summary">
          <span>{usageLine ?? '该节点尚无带回用量的运行记录。'}</span>
          {costLine ? (
            <strong>{costLine}</strong>
          ) : (
            <span className="muted">当前通道未回报金额，仅统计 token。</span>
          )}
        </div>
      </section>

      <section className="detail-section">
        <h3>
          <ShieldCheck size={16} />
          运行记录
        </h3>
        <div className="run-list">
          {detail.runs.map((run) => (
            <div className="run" key={run.id}>
              <div>
                <strong>{runKindLabels[run.kind] ?? run.kind}</strong>
                <span>{runStatusLabels[run.status] ?? run.status}</span>
              </div>
              <code>{run.command}</code>
              <div className="run-debug-grid" aria-label="运行调试信息">
                <span>工作目录</span>
                <code>{run.workdir ?? '无'}</code>
                <span>宪法版本</span>
                <code>{run.constitution_hash ?? '未绑定'}</code>
                <span>Prompt</span>
                <code>{run.prompt_path ?? '无'}</code>
                <span>日志</span>
                <code>{run.logs_path ?? '无'}</code>
                <span>结果文件</span>
                <code>{run.last_message_path ?? '无'}</code>
              </div>
              <div className="artifact-actions" aria-label="Codex 调试文件">
                <button
                  className={artifactKey === `${run.id}:prompt` ? 'active' : ''}
                  disabled={!run.prompt_path || artifactLoading}
                  onClick={() => loadArtifact(run.id, 'prompt')}
                  type="button"
                  title="查看 Prompt"
                >
                  <FileText size={14} />
                  Prompt
                </button>
                <button
                  className={artifactKey === `${run.id}:logs` ? 'active' : ''}
                  disabled={!run.logs_path || artifactLoading}
                  onClick={() => loadArtifact(run.id, 'logs')}
                  type="button"
                  title="查看日志"
                >
                  <Terminal size={14} />
                  日志
                </button>
                <button
                  className={artifactKey === `${run.id}:last_message` ? 'active' : ''}
                  disabled={!run.last_message_path || artifactLoading}
                  onClick={() => loadArtifact(run.id, 'last_message')}
                  type="button"
                  title="查看最后消息"
                >
                  <FileText size={14} />
                  结果
                </button>
              </div>
              {artifactKey?.startsWith(`${run.id}:`) && (
                <div className="artifact-viewer">
                  <div className="artifact-head">
                    <strong>{artifact?.kind ?? '调试文件'}</strong>
                    <span>
                      {artifactLoading
                        ? '加载中'
                        : artifact?.exists
                          ? artifact.truncated
                            ? '已截断'
                            : '完整'
                          : '未找到'}
                    </span>
                  </div>
                  {artifact?.path && <code className="artifact-path">{artifact.path}</code>}
                  {artifactError ? (
                    <p className="artifact-error">{artifactError}</p>
                  ) : (
                    <>
                      <pre>{artifactLoading ? '正在读取调试文件...' : artifact?.content || '没有可预览的内容。'}</pre>
                      {artifactLoading && <BusyRunner label="正在读取调试文件..." compact />}
                    </>
                  )}
                </div>
              )}
              {run.result && <code className="json-block">{JSON.stringify(run.result, null, 2)}</code>}
            </div>
          ))}
          {detail.runs.length === 0 && <p className="muted">暂无运行记录。</p>}
        </div>
      </section>

      <section className="detail-section">
        <h3>事件</h3>
        <div className="event-list">
          {detail.events.slice().reverse().map((event) => (
            <div className="event" key={event.id}>
              <span>{new Date(event.created_at).toLocaleTimeString()}</span>
              <strong>{eventTypeLabels[event.type] ?? event.type}</strong>
              <p>{event.message}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function WatchdogPanel({
  state,
  layout,
  busy,
  result,
  onInstall,
  onRunOnce,
  onToggleDrain,
}: {
  state: WatchdogStateResponse | null;
  layout: StateLayoutResponse | null;
  busy: boolean;
  result: string | null;
  onInstall: () => void;
  onRunOnce: () => void;
  onToggleDrain: () => void;
}) {
  if (!state) {
    return (
      <section className="watchdog-panel" aria-label="自动续跑状态">
        <div className="watchdog-head">
          <h3>
            <Clock size={15} />
            自动续跑
          </h3>
          <span>等待状态</span>
        </div>
        <p>正在等待 API 返回看门狗状态。</p>
        <div className="watchdog-actions">
          <button className="secondary" onClick={onInstall} disabled={busy} type="button" title="安装或更新 Windows 定时巡检和登录自启钩子">
            <ShieldCheck size={14} />
            安装钩子
          </button>
          <button className="secondary" onClick={onRunOnce} disabled={busy} type="button" title="立即执行一次本地 watchdog 巡检">
            <RefreshCw size={14} />
            巡检一次
          </button>
        </div>
        {result && <p className="watchdog-result">{result}</p>}
      </section>
    );
  }
  const secondsLeft = secondsUntil(state.resume_at);
  const actionLabel = watchdogActionLabels[state.recommended_action] ?? state.recommended_action;
  const rootStatus = state.root_status ? (statusLabels[state.root_status] ?? state.root_status) : '无根节点';
  const budgetValue =
    state.agent_burst_limit > 0 ? `${state.agent_burst_used}/${state.agent_burst_work_limit}` : '不限';
  const reserveValue = state.agent_burst_limit > 0 ? `${state.agent_burst_reserve} 次` : '未启用';
  const goalModeValue = state.goal_mode ? '长跑' : '保守';
  const budgetWarningLabel =
    state.budget_warning_level === 'paused'
      ? '已暂停'
      : state.budget_warning_level === 'warning'
        ? '预警'
        : state.budget_warning_level === 'sleeping'
          ? '休眠中'
        : state.budget_warning_level === 'goal'
          ? '长跑'
        : state.budget_warning_level === 'resource_pressure'
          ? '资源压力'
          : '正常';
  const scheduledTaskValue = state.scheduled_task_exists ? '已安装' : '可安装';
  const startupHookValue =
    state.logon_task_exists
      ? '计划任务已安装'
      : state.startup_hook_exists || state.startup_hook_mode === 'startup_folder'
        ? 'Startup 已安装'
        : '可安装';
  const intervalValue = `${state.watchdog_interval_minutes} 分钟`;
  const apiRestartPending = state.api_restart_pending === true;
  const apiRestartDrainArmed = state.api_restart_drain_armed === true;
  const apiRestartFlagPath = state.api_restart_flag_path || '等待新版 API 返回安全重启路径';
  const apiRestartDrainPath = state.api_restart_drain_flag_path || '等待新版 API 返回维护排空路径';
  const supportsApiRestartDrain = typeof state.api_restart_drain_armed === 'boolean' && Boolean(state.api_restart_drain_flag_path);
  const apiRestartStatusLabel = !supportsApiRestartDrain
    ? '待新版接口'
    : apiRestartDrainArmed
      ? '维护排空'
      : apiRestartPending
        ? '等待空闲'
        : '已加载';
  const quotaObservation =
    state.codex_quota_observability || '旧 API 暂未返回额度观测字段，等待安全重启后刷新。';
  const rawActiveSlots = state.codex_slots_active ?? state.active_agents;
  const slotCapacityValue = state.codex_slots_capacity ?? rawActiveSlots;
  const activeSlots = displaySlotCount(rawActiveSlots, slotCapacityValue);
  const activeStatusNodes = state.active_agent_statuses ?? state.active_agents;
  const observedActiveUpperBound = state.active_agents;
  const slotCapacity = state.codex_slots_capacity ?? '?';
  const effectiveSlotNote = rawActiveSlots > activeSlots ? ` · 有效占用 ${rawActiveSlots}（含预约）` : '';
  const quotaRetryAfter =
    typeof state.codex_quota_retry_after_seconds === 'number' && state.codex_quota_retry_after_seconds > 0
      ? formatDuration(state.codex_quota_retry_after_seconds)
      : null;
  const quotaResetAt = state.codex_quota_reset_at ? formatDateTime(state.codex_quota_reset_at) : null;
  const quotaDetail = quotaResetAt
    ? `真实退避信号：${quotaResetAt}${quotaRetryAfter ? ` 后可重试，等待 ${quotaRetryAfter}` : ''}`
    : quotaObservation === 'codex_cli_has_no_direct_quota_or_5h_reset_api' ||
        quotaObservation === 'codex_cli_no_direct_quota_endpoint'
      ? 'CLI 没有暴露可直接读取的额度刷新时间；L4 只使用实际限流输出、退避字段和当前队列压力。'
      : quotaObservation;
  const healthSummary = state.codex_health_summary || '旧 API 暂未返回预检摘要，等待安全重启后刷新。';
  const codexHealthLabel =
    state.codex_health_status === 'ok'
      ? '正常'
      : state.codex_health_status === 'warning'
        ? '有告警'
        : state.codex_health_status === 'resource_block'
          ? '限流等待'
          : state.codex_health_status === 'blocked'
            ? '已拦截'
            : state.codex_health_exists
              ? state.codex_health_status
              : '未预检';
  const hookState = state.should_resume
    ? '可触发'
    : rawActiveSlots > 0 || activeStatusNodes > 0
      ? '观察中'
      : state.hibernating > 0
        ? '已布防'
        : state.active_frontier > 0
          ? '待巡检'
          : '空闲';
  const guardianStatus = state.guardian_status ?? 'unknown';
  const guardianRisk = state.guardian_risk_level ?? 'watch';
  const guardianChecks = (state.guardian_checks ?? []) as GuardianCheck[];
  const guardianSummary = state.guardian_summary || 'L4 监护者正在等待下一次状态刷新。';
  const guardianMode = state.guardian_mode || state.recommended_action;
  const fuelMode = state.l4_fuel_mode || 'pending';
  const fuelPhase = state.l4_fuel_phase || 'pending';
  const fuelTarget =
    typeof state.l4_fuel_target === 'number'
      ? state.l4_fuel_target
      : (state.codex_slots_capacity ?? state.active_agents);
  const fuelCapacity =
    typeof state.l4_fuel_current_capacity === 'number'
      ? state.l4_fuel_current_capacity
      : (state.codex_slots_capacity ?? fuelTarget);
  const fuelSlots = `${displaySlotCount(rawActiveSlots, fuelCapacity)}/${fuelCapacity}`;
  const fuelScore = formatPercent(state.l4_fuel_score);
  const fuelQueuePressure = formatPercent(state.l4_fuel_queue_pressure);
  const fuelFrontierPressure = formatPercent(state.l4_fuel_frontier_pressure);
  const fuelTimePressure = formatPercent(state.l4_fuel_time_pressure);
  const fuelRecommendedTarget =
    typeof state.l4_fuel_recommended_target === 'number' ? state.l4_fuel_recommended_target : fuelTarget;
  const fuelOpenFrontier =
    typeof state.l4_fuel_open_frontier === 'number' ? state.l4_fuel_open_frontier : state.active_frontier;
  const fuelNextDeadline = formatDateTime(state.l4_fuel_next_deadline_local ?? state.l4_fuel_deadline_local);
  const fuelMinutes = formatDuration(
    typeof state.l4_fuel_minutes_to_deadline === 'number' ? state.l4_fuel_minutes_to_deadline * 60 : null,
  );
  const fuelQuotaReset = state.l4_fuel_quota_reset_at
    ? formatDateTime(state.l4_fuel_quota_reset_at)
    : (quotaResetAt ?? 'not observed');
  const guardianStatePath = state.guardian_state_path || '等待新版 API 写入 l4_guardian_state.json';
  return (
    <section className={`watchdog-panel ${state.should_resume ? 'ready' : ''}`} aria-label="自动续跑状态">
      <div className="watchdog-head">
        <h3>
          <Clock size={15} />
          L4 监护者
        </h3>
        <span>{actionLabel}</span>
      </div>
      {/*
        状态布局：**当前进程到底在用哪个工程/哪个库**。
        状态与工程解耦之后最危险的错误是"看着是这个工程、其实在改另一个"——
        路径都对、也不报错，只有库里的内容不对。摆在这里就让错位一眼可见。
        快照与 active.json 不一致时明确提示要重启（决策 D1：不热切库）。
      */}
      {layout && (
        <div
          className={`state-layout-strip ${layout.snapshot_matches_active_file ? '' : 'stale'}`}
          aria-label="状态布局"
        >
          <code>
            {layout.bound
              ? `工程：${layout.project_root ?? '（未知）'}`
              : '未绑定工程（每工程状态回退仓库内 workspace）'}
          </code>
          {layout.bound && <code>库：{layout.db_path ?? layout.database_url}</code>}
          <code>状态根：{layout.state_home}</code>
          {!layout.snapshot_matches_active_file && (
            <code className="state-layout-warning">
              ⚠️ 磁盘上的 active.json 已改为
              {layout.active_file_root || layout.active_file_key || '（空）'}
              ，但本进程仍在用启动时的工程 —— 请重启 API（不会热切库）
            </code>
          )}
        </div>
      )}
      <div className={`guardian-strip ${guardianRisk}`}>
        <div>
          <small>
            {(state.guardian_level ?? 'L4')} · {guardianStatusLabels[guardianStatus] ?? guardianStatus} · 风险：
            {guardianRiskLabels[guardianRisk] ?? guardianRisk}
          </small>
          <strong>{guardianSummary}</strong>
        </div>
        <span>{guardianMode}</span>
      </div>
      {guardianChecks.length > 0 && (
        <div className="guardian-checks" aria-label="L4 监护检查">
          {guardianChecks.slice(0, 6).map((check) => (
            <article className={`guardian-check ${check.status}`} key={check.key}>
              <span>{check.label}</span>
              <b>{guardianCheckLabels[check.status] ?? check.status}</b>
              <p>{check.detail}</p>
            </article>
          ))}
        </div>
      )}
      <div className="l4-fuel-panel" aria-label="L4 fuel control">
        <div>
          <small>L4 fuel</small>
          <strong>{autoConcurrencyLabel(fuelMode)}</strong>
        </div>
        <div>
          <small>slots</small>
          <strong>{fuelSlots}</strong>
        </div>
        <div>
          <small>target</small>
          <strong>{fuelTarget}</strong>
        </div>
        <div>
          <small>suggested</small>
          <strong>{fuelRecommendedTarget}</strong>
        </div>
        <div>
          <small>phase</small>
          <strong>{fuelPhase}</strong>
        </div>
        <div>
          <small>score</small>
          <strong>{fuelScore}</strong>
        </div>
        <div>
          <small>queue</small>
          <strong>{fuelQueuePressure}</strong>
        </div>
        <div>
          <small>frontier</small>
          <strong>{fuelFrontierPressure}</strong>
        </div>
        <div>
          <small>open front</small>
          <strong>{fuelOpenFrontier}</strong>
        </div>
        <div>
          <small>time pressure</small>
          <strong>{fuelTimePressure}</strong>
        </div>
        <div>
          <small>next fuel point</small>
          <strong>{fuelNextDeadline}</strong>
        </div>
        <div>
          <small>time left</small>
          <strong>{fuelMinutes}</strong>
        </div>
        <div>
          <small>quota reset</small>
          <strong>{fuelQuotaReset}</strong>
        </div>
      </div>
      <div className="watchdog-grid">
        <div>
          <small>根节点状态</small>
          <strong>{rootStatus}</strong>
        </div>
        <div>
          <small>启动预算</small>
          <strong>{budgetValue}</strong>
        </div>
        <div>
          <small>预算预警</small>
          <strong>{budgetWarningLabel}</strong>
        </div>
        <div>
          <small>Goal 模式</small>
          <strong>{goalModeValue}</strong>
        </div>
        <div>
          <small>自动保留</small>
          <strong>{reserveValue}</strong>
        </div>
        <div>
          <small>钩子状态</small>
          <strong>{hookState}</strong>
        </div>
        <div>
          <small>巡检任务</small>
          <strong>{scheduledTaskValue}</strong>
        </div>
        <div>
          <small>巡检周期</small>
          <strong>{intervalValue}</strong>
        </div>
        <div>
          <small>登录自启</small>
          <strong>{startupHookValue}</strong>
        </div>
        <div>
          <small>Codex 预检</small>
          <strong>{codexHealthLabel}</strong>
        </div>
        <div>
          <small>API 更新</small>
          <strong>{apiRestartStatusLabel}</strong>
        </div>
        <div>
          <small>恢复时间</small>
          <strong>{formatDateTime(state.resume_at)}</strong>
        </div>
        <div>
          <small>剩余等待</small>
          <strong>{formatDuration(secondsLeft)}</strong>
        </div>
      </div>
      <div className="watchdog-actions">
        <button className="secondary" onClick={onInstall} disabled={busy} type="button" title="安装或更新 Windows 定时巡检和登录自启钩子">
          <ShieldCheck size={14} />
          安装/更新钩子
        </button>
        <button className="secondary" onClick={onRunOnce} disabled={busy} type="button" title="立即执行一次本地 watchdog 巡检；未到恢复时间时不会启动真实 Codex agent">
          <RefreshCw size={14} />
          手动巡检
        </button>
        {supportsApiRestartDrain && (
          <button
            className={`secondary ${apiRestartDrainArmed ? 'danger-soft' : ''}`}
            onClick={onToggleDrain}
            disabled={busy}
            type="button"
            title={
              apiRestartDrainArmed
                ? '取消维护排空；API 继续等待任务自然空闲后重启'
                : '武装维护排空；watchdog 会短暂休眠当前任务树，等活跃 agent 归零后安全重启 API'
            }
          >
            <Pause size={14} />
            {apiRestartDrainArmed ? '取消排空' : '维护排空'}
          </button>
        )}
      </div>
      {result && <p className="watchdog-result">{result}</p>}
      <p className={`budget-warning ${state.budget_warning_level}`}>{state.budget_warning_message}</p>
      {state.active_resource_pressure && (
        <p className="budget-warning resource_pressure">
          活跃 Agent 已报告限流/额度压力：{state.active_resource_pressure_reason || '等待下一次 watchdog 采样确认。'}
        </p>
      )}
      <p>{state.token_reserve_summary}</p>
      <p>
        Codex 额度观测：{quotaObservation === 'codex_cli_has_no_direct_quota_or_5h_reset_api'
          ? 'CLI 暂无直接读取 5 小时额度/刷新时间的接口，watchdog 依靠限流输出与退避时间自救。'
          : quotaObservation}
      </p>
      <p>最近预检：{healthSummary}</p>
      <p className="quota-observation">{quotaDetail}</p>
      {state.codex_quota_signal && <p className="quota-observation">Codex quota signal: {state.codex_quota_signal}</p>}
      <p className="quota-observation">
        Codex 额度：当前 Codex CLI 不暴露剩余额度数字；L4 监护者只能根据登录状态、限流日志、失败原因和退避时间推断是否该低功耗。
      </p>
      <p>{state.reason || '暂无休眠原因。'}</p>
      <code>监护快照：{guardianStatePath}</code>
      <code>
        休眠 {state.hibernating} · 真实槽位 {activeSlots}/{slotCapacity}{effectiveSlotNote} · 状态节点 {activeStatusNodes} · 观测上界 {observedActiveUpperBound}（不参与调速） · 前线 {state.active_frontier}
      </code>
      <code>
        API 安全重启：{supportsApiRestartDrain
          ? apiRestartPending
            ? `等待真实槽位和状态节点归零 · ${apiRestartFlagPath}`
            : '无待加载更新'
          : '旧 API 进程尚未暴露新版状态字段；watchdog 会继续按本地 flag 处理'}
      </code>
      <code>
        维护排空：{supportsApiRestartDrain
          ? apiRestartDrainArmed
            ? `已武装 · watchdog 可短暂休眠后重启 · ${apiRestartDrainPath}`
            : `未武装 · 可用面板按钮开启，或创建 ${apiRestartDrainPath}`
          : '等待 API 加载新版维护排空接口'}
      </code>
      <code>Codex 预检：{state.codex_health_checked_at ? formatDateTime(state.codex_health_checked_at) : '未执行'} · {state.codex_health_path}</code>
      <code>日志：{state.watchdog_log_path}</code>
      {state.scheduled_task_path && <code>周期巡检任务：{state.scheduled_task_path}</code>}
      {state.logon_task_path && <code>登录计划任务：{state.logon_task_path}</code>}
      {state.startup_hook_path && <code>登录钩子：{state.startup_hook_path}</code>}
      {(state.active_agent_diagnostics ?? []).length > 0 && (
        <div className="agent-diagnostics" aria-label="运行中 Agent 诊断">
          <strong>运行中 Agent 诊断</strong>
          {(state.active_agent_diagnostics ?? []).map((item) => (
            <article className={`agent-diagnostic ${item.severity}`} key={item.task_id}>
              <div>
                <span>
                  第 {item.depth} 层 · {item.phase} · {statusLabels[item.status as TaskStatus] ?? item.status}
                </span>
                <b>{item.title}</b>
              </div>
              <p>{item.summary}</p>
              <small>
                无新输出 {formatDuration(item.no_progress_seconds)} / 空闲看门狗 {formatDuration(item.idle_timeout_seconds)}
              </small>
              {item.markers.length > 0 && <small>信号：{item.markers.join(' / ')}</small>}
              {item.latest_progress_at && <small>最近输出：{formatDateTime(item.latest_progress_at)}</small>}
            </article>
          ))}
        </div>
      )}
      <code className="watchdog-command">安装/更新自启动：{state.watchdog_install_command}</code>
      <code className="watchdog-command">手动巡检一次：{state.watchdog_run_command}</code>
    </section>
  );
}

function ConstitutionPanel({
  constitution,
  board,
}: {
  constitution: ConstitutionResponse | null;
  board: ConstitutionBoard | null;
}) {
  if (!constitution) {
    return (
      <section className="constitution-panel" aria-label="项目宪法">
        <div className="constitution-head">
          <strong>项目宪法</strong>
          <span>未生成</span>
        </div>
        <p>启动任务时会先生成并锁定宪法，再让所有 agent 继承。</p>
      </section>
    );
  }

  const floor = stringList(constitution.data.quality_floor).slice(0, 3);
  const ceiling = stringList(constitution.data.quality_ceiling).slice(0, 3);
  return (
    <section className="constitution-panel locked" aria-label="项目宪法">
      <div className="constitution-head">
        <strong>项目宪法</strong>
        <span>{constitution.status === 'locked' ? '已锁定' : '草案'}</span>
      </div>
      <p>
        {String(constitution.data.quality_tier ?? '垂直切片')} · v{constitution.version} · {constitution.content_hash}
      </p>
      {board ? (
        <div className="constitution-propagation">
          {/* 传播状态是判据不是装饰：它回答"这一版真的铺到全树了吗" */}
          <p className="constitution-propagation-headline">{constitutionPropagationHeadline(board)}</p>
          <p>
            {versionDistributionEntries(board)
              .map((entry) => `${entry.version === 'unknown' ? '未登记' : `v${entry.version}`} × ${entry.count}`)
              .join(' · ')}
          </p>
          {board.last_amendment ? (
            <p>
              最近修订 v{board.last_amendment.from_version} → v{board.last_amendment.to_version} ·{' '}
              {board.last_amendment.direction ?? '未声明方向'}
              {board.last_amendment.actor ? ` · ${board.last_amendment.actor}` : ''}
            </p>
          ) : (
            <p>尚未修订过。</p>
          )}
        </div>
      ) : null}
      <h3>品质下限</h3>
      <ul>
        {floor.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <h3>规模上限</h3>
      <ul>
        {ceiling.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

function ObligationBoardPanel({ board }: { board: ObligationBoard | null }) {
  if (!board) return null;
  const blocking = board.blocking_total > 0;
  return (
    <section className={`obligation-panel ${blocking ? 'blocked' : 'clear'}`} aria-label="未清债务">
      <div className="obligation-head">
        <strong>未清债务</strong>
        <span>{obligationBoardHeadline(board)}</span>
      </div>
      {blocking ? (
        <ul>
          {obligationTypeBreakdown(board)
            .filter((entry) => entry.count > 0)
            .map((entry) => (
              <li key={entry.type}>
                {entry.label} × {entry.count}
              </li>
            ))}
        </ul>
      ) : (
        <p>没有被债务挡住的事项。</p>
      )}
      <p className="obligation-meta">
        待再验证 {board.revalidate_queue_depth} · 人工清债 {board.settled_by_human_count} · 已清 {board.settled_total}
      </p>
    </section>
  );
}

// 跨树债务索引是**派生缓存**：`built_at` / `in_sync` 决定这个数能不能当读数用。
// 读不出来时必须明说（并把重建命令给出来），否则 0 会被读成"没有债"——方向正好反了。
function ObligationIndexPanel({ board }: { board: ObligationIndexBoard | null }) {
  if (!board) return null;
  const readable = obligationIndexIsReadable(board);
  return (
    <section className={`obligation-panel ${readable ? 'clear' : 'blocked'}`} aria-label="跨树债务索引">
      <div className="obligation-head">
        <strong>跨树债务索引</strong>
        <span>{obligationIndexHeadline(board)}</span>
      </div>
      {board.total > 0 ? (
        <ul>
          {obligationIndexTypeBreakdown(board).map((entry) => (
            <li key={entry.type}>
              {entry.label} × {entry.count}
            </li>
          ))}
        </ul>
      ) : (
        <p>{readable ? '这个范围里没有债务。' : '索引不可信，这里的 0 不代表没有债。'}</p>
      )}
      <p className="obligation-meta">
        {board.built_at ? `快照 ${formatDateTime(board.built_at)}` : '表里一行都没有'} · 索引 {board.indexed} /
        真相 {board.projected} · 覆盖 {Object.keys(board.by_root).length} 棵树 · 重建：{board.rebuild_command}
      </p>
    </section>
  );
}

function BusyRunner({ label, compact = false }: { label: string; compact?: boolean }) {
  return (
    <div className={`busy-runner ${compact ? 'compact' : ''}`} role="status" aria-live="polite">
      <span className="busy-spinner" aria-hidden="true" />
      <span className="busy-runner-label">{label}</span>
      <span className="busy-marquee" aria-hidden="true">
        <i />
      </span>
    </div>
  );
}

function Section({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return (
    <section className="detail-section">
      <h3>{title}</h3>
      {items.length > 0 ? (
        <ul>
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="muted">{empty}</p>
      )}
    </section>
  );
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item))
    : [];
}

function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function nodeBlockSummary(task: BattlefieldTask, taskById?: Map<string, BattlefieldTask>, seen: Set<string> = new Set()): string | null {
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
        const fallback = dependency.error_message || (dependency.status === 'done' ? '已完成' : `状态为 ${statusLabels[dependency.status] ?? dependency.status}`);
        return `依赖「${dependency.title}」${statusLabels[dependency.status] ?? dependency.status}：${traced ?? fallback}`;
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

function readStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === 'string');
}

function taskEvidenceBadges(task: BattlefieldTask): { evidence: number; changed: number; tests: number } {
  const result = (task.result ?? {}) as Record<string, unknown>;
  const acceptance = (result.acceptance ?? {}) as Record<string, unknown>;
  return {
    evidence: readStringList(result.evidence).length + readStringList(acceptance.evidence).length,
    changed: readStringList(result.changed_files).length,
    tests: readStringList(result.tests).length,
  };
}

function summarizeBlockedNodes(tasks: BattlefieldTask[]): {
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

function buildWaitingReports(tasks: BattlefieldTask[]): Map<string, Record<string, unknown>> {
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

function parseJsonObject(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('宪法结构 JSON 必须是对象。');
  }
  return value as Record<string, unknown>;
}

function Stat({ label, value, title }: { label: string; value: number | string; title?: string }) {
  return (
    <div className="stat" title={title}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
