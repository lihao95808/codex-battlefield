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
import { NodeLod, summarizeBlockedNodes, parseJsonObject } from './taskView';
import { type Node } from '@xyflow/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { cancelAllProjects, draftConstitution, fetchLatestConstitution, fetchObligationIndex, fetchStateLayout, fetchTask, fetchTree, fetchWatchdogState, installWatchdog, lockConstitution, postTaskAction, runWatchdogOnce, setApiRestartDrain, setBudgetGuard, startOneShotProject, startProject, updateConstitution } from './api';

import { DEFAULT_GOAL } from './labels';
import { displaySlotCount, statNumber, summarizeWatchdogAction } from './format';

import { useBattlefieldDerived } from './useBattlefieldDerived';
import { useTreeRefresh } from './useTreeRefresh';
import type { ConstitutionResponse, ObligationIndexBoard, TaskDetailResponse, TaskStatus, TreeResponse, StateLayoutResponse, WatchdogStateResponse } from './types';

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

  // `selectedId` 用 ref 读，**不进 `loadTree` 的依赖数组**：它每次加载都会被
  // `setSelectedId` 改一次，而 `loadTree` 的标识一变，`useTreeRefresh` 里那个
  // "挂载时拉一次"的 effect 就会**再拉一次** —— 实测千节点首屏因此打了两个
  // 完全相同的 `/api/tree`（1264ms + 519ms，URL 逐字相同）。
  const selectedIdRef = useRef(selectedId);
  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

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
      const currentSelectedId = selectedIdRef.current;
      const selectedStillExists = currentSelectedId
        ? next.tasks.some((task) => task.id === currentSelectedId)
        : false;
      if (!selectedStillExists) {
        setSelectedId(next.tasks[0]?.id ?? null);
      }
    } finally {
      treeLoadInFlight.current = false;
    }
  }, [compactDoneArchive, expandedDoneArchiveParentIds, rootId]);

  useTreeRefresh({ loadTree, onError: setError });

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
    let alive = true;
    fetchTask(selectedId)
      .then((value) => { if (alive) setDetail(value); })
      .catch(() => { if (alive) setDetail(null); });
    return () => { alive = false; };
  }, [selectedId, tree]);


  const derived = useBattlefieldDerived({
    tree,
    statusFilter,
    collapsedNodeIds,
    compactDoneArchive,
    expandedDoneArchiveParentIds,
    visibleDepthLimit,
    nodeLod,
    selectedId,
    setCollapsedNodeIds,
    setExpandedDoneArchiveParentIds,
    setVisibleDepthLimit,
    setNodeLod,
    setNodes,
  });
  const {
    taskById,
    parentIds,
    waitingReports,
    doneArchive,
    availableDepths,
    maxDepth,
    effectiveDepthLimit,
    depthDisplayLabel,
    visibleTasks,
    toggleNodeCollapse,
    expandAll,
    collapseOneLayer,
    expandOneLayer,
    handleViewportMove,
    edges,
  } = derived;

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


  return { collapsedNodeIds,
    ...derived,
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
