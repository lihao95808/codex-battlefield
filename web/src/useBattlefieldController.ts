/**
 * `App()` 的状态与回调（P5-2：`App.tsx` 拆分的第二步）。
 *
 * 为什么逻辑要单独成 hook：`App()` 自己 1037 行，其中约 690 行是状态/回调/派生值。
 * 只把 JSX 搬出去最多降到 ~900 行；把逻辑搬进 hook 之后 `App.tsx` 只剩组合，
 * 才能真的回到 500 行以内。**这里只搬家，没有改行为。**
 *
 * 用法（`App.tsx` 里）：`const view = useBattlefieldController();`，整棵 JSX 通过
 * `view.xxx` 读它。刻意**不做** `const { a, b, ... } = ...`：那个解构语句本身会有
 * 一两百行，等于把搬走的行数又搬回来一部分。
 */
import { rootIdFromUrl, writeRootIdToUrl, previewUrlForRoot, openPendingPreviewWindow, navigatePreviewWindow } from './browserSession';
import { edgeStateStyles, liveNodeStatuses, NodeLod, nodeLodForZoom, TaskNodeData, asRecord, summarizeBlockedNodes, buildWaitingReports, parseJsonObject } from './taskView';
import { MarkerType, Position, type Edge, type Node, type Viewport } from '@xyflow/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cancelAllProjects, draftConstitution, fetchLatestConstitution, fetchObligationIndex, fetchStateLayout, fetchTask, fetchTree, fetchWatchdogState, installWatchdog, lockConstitution, postTaskAction, runWatchdogOnce, setApiRestartDrain, setBudgetGuard, startOneShotProject, startProject, updateConstitution } from './api';
import { layoutBattlefield } from './layout';
import { DEFAULT_GOAL } from './labels';
import { displaySlotCount, statNumber, summarizeWatchdogAction } from './format';
import { createPoller } from './polling';
import type { BattlefieldTask, ConstitutionResponse, ObligationIndexBoard, TaskDetailResponse, TaskStatus, TreeResponse, StateLayoutResponse, WatchdogStateResponse } from './types';

export function useBattlefieldController() {
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

    // 事件流是**主刷新通路**（P5-2）：树有变化时后端推事件，这里 2 秒后拉一次。
    // 保留这个防抖是因为一次状态转换往往连着好几条事件，逐条拉会打成一片。
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

    // 轮询是**兜底**，不是主力：SSE 静默断开时（代理掐连接、网络抖动）没有它
    // 界面会一直显示旧数据而没人知道。所以留着，但把间隔从 10 秒放到 60 秒 ——
    // 而那 10 秒一次的开销才是真的浪费（`/api/tree` 在千节点下要 130ms）。
    //
    // 并且**页面不可见时完全不拉**（`createPoller`）：原来整晚开着没人看的标签页
    // 仍然每 10 秒打一次 API。切回可见时立刻拉一次，所以看不到旧数据。
    const poller = createPoller({
      tick: () => loadTree().catch(() => undefined),
      intervalMs: 60000,
    });
    const detachVisibility = poller.attachVisibility(
      typeof document === 'undefined' ? {} : document,
    );

    return () => {
      detachVisibility();
      poller.stop();
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


  return {
    activeConstitution,
    activeRootId,
    availableDepths,
    blockedSummary,
    busy,
    busyLabel,
    canEditConstitution,
    collapseOneLayer,
    constitutionDataText,
    constitutionLocked,
    constitutionMarkdown,
    constitutionMatchesInput,
    depthDisplayLabel,
    detail,
    doneArchive,
    edges,
    effectiveDepthLimit,
    error,
    expandAll,
    expandOneLayer,
    goal,
    handleViewportMove,
    hasActiveFlow,
    layoutState,
    loadTree,
    maxDepth,
    nodeLod,
    nodes,
    obligationIndex,
    onAction,
    onCancelAll,
    onDraftConstitution,
    onHibernateRoot,
    onInstallWatchdog,
    onLockConstitution,
    onOneShotStart,
    onRunWatchdogOnce,
    onSaveConstitution,
    onStart,
    onToggleApiRestartDrain,
    parentIds,
    runnerMode,
    selectedId,
    setConstitutionDataText,
    setConstitutionMarkdown,
    setExpandedDoneArchiveParentIds,
    setGoal,
    setNodeLod,
    setRunnerMode,
    setSelectedId,
    setStatusFilter,
    setTargetPath,
    setVisibleDepthLimit,
    statusFilter,
    targetPath,
    taskById,
    toggleNodeCollapse,
    tree,
    treeConstitution,
    treeIsStale,
    visibleActiveSlots,
    visibleDepthLimit,
    visibleNodeCount,
    visibleSlotCapacity,
    visibleStats,
    waitingReports,
    watchdogResult,
    watchdogState,
  };
}
