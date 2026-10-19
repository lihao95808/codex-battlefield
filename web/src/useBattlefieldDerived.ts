/**
 * `useBattlefieldController` 的**派生层**（P5-2：从那个 783 行的 hook 里抽出来）。
 *
 * 它只做一件事：把状态算成视图要的数据（可见任务、边、归档摘要、深度、统计……）。
 * 输入是状态与 setter，输出是 `useMemo`/`useCallback` 的结果 —— **没有副作用**，
 * 所以它可以独立测，也可以被别的视图复用。
 *
 * 输入清单是用 `web/_ts_free_ids.mjs`（TypeScript 解析器）算出来的，不是靠眼睛。
 */
import type { Dispatch, SetStateAction } from 'react';

import { layoutBattlefield } from './layout';
import { statNumber } from './format';
import { edgeStateStyles, liveNodeStatuses, nodeLodForZoom, type NodeLod, type TaskNodeData, asRecord, buildWaitingReports } from './taskView';
import { windowNotice, windowTasks } from './virtualization';
import type { BattlefieldTask, TaskStatus, TreeResponse } from './types';
import { MarkerType, Position, type Edge, type Node, type Viewport } from '@xyflow/react';
import { useCallback, useEffect, useMemo } from 'react';

export function useBattlefieldDerived({
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
}: {
  tree: TreeResponse | null;
  statusFilter: TaskStatus | 'all' | 'active';
  collapsedNodeIds: Set<string>;
  compactDoneArchive: boolean;
  expandedDoneArchiveParentIds: Set<string>;
  visibleDepthLimit: number | null;
  nodeLod: NodeLod;
  selectedId: string | null;
  setCollapsedNodeIds: Dispatch<SetStateAction<Set<string>>>;
  setExpandedDoneArchiveParentIds: Dispatch<SetStateAction<Set<string>>>;
  setVisibleDepthLimit: Dispatch<SetStateAction<number | null>>;
  setNodeLod: Dispatch<SetStateAction<NodeLod>>;
  setNodes: Dispatch<SetStateAction<Node[]>>;
}) {
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
    const selectedPath = new Set<string>();
    let selected = selectedId ? taskById.get(selectedId) : undefined;
    while (selected && !selectedPath.has(selected.id)) {
      selectedPath.add(selected.id); selected = selected.parent_id ? taskById.get(selected.parent_id) : undefined;
    }
    return tree.tasks.filter((task) => {
      if (selectedPath.has(task.id)) return true;
      if (doneArchive.hiddenIds.has(task.id)) return false;
      if (isHiddenByCollapse(task)) return false;
      if (statusFilter === 'all') return true;
      if (statusFilter === 'active') return task.is_active_frontier;
      return task.status === statusFilter;
    });
  }, [doneArchive.hiddenIds, isHiddenByCollapse, statusFilter, tree, selectedId, taskById]);

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

  // 长列表虚拟化（P5-2 最后一项）：**布局与渲染**只用有界窗口。
  //
  // 为什么在布局之前切：真正的代价不是 DOM 数量，而是把节点交给 `@xyflow/react`
  // 之前那一步 `layoutBattlefield(...)`（elkjs）要逐节点算坐标 —— 千节点时它是主线程
  // 上的大头，而 ReactFlow 自己的 `onlyRenderVisibleElements` 管不到它。
  //
  // 为什么 `visibleNodeCount` 仍用**完整**的 `visibleTasks`：显示"可见节点"时不减，
  // 旁边单独显示"本次渲染了多少"。把统计也改成窗口大小，就等于悄悄改掉了用户读到的数
  // —— 那是"用显示数字掩盖省略"。
  const taskWindow = useMemo(
    () => windowTasks(visibleTasks, { selectedId }),
    [selectedId, visibleTasks],
  );
  const renderedTasks = taskWindow.tasks;

  const edges = useMemo<Edge[]>(() => {
    if (!tree) return [];
    // 边也跟着窗口走：否则会出现"两端节点没渲染、但线段还在"的悬空边。
    const visibleIds = new Set(renderedTasks.map((task) => task.id));
    const taskById = new Map(renderedTasks.map((task) => [task.id, task]));
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
  }, [liveSpineEdgeIds, renderedTasks, selectedId, tree]);

  useEffect(() => {
    const nextNodes: Node[] = renderedTasks.map((task) => ({
      id: task.id,
      type: 'task',
      selected: task.id === selectedId,
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
    renderedTasks,
    selectedId,
    taskById,
    toggleDoneArchive,
    toggleNodeCollapse,
    visibleDepthLimit,
    waitingReports,
  ]);

  return {
    taskById,
    parentIds,
    waitingReports,
    childrenByParent,
    descendantCounts,
    compactDoneArchiveActive,
    doneArchive,
    availableDepths,
    maxDepth,
    effectiveDepthLimit,
    depthDisplayLabel,
    isHiddenByCollapse,
    visibleTasks,
    // 虚拟化：界面用 `renderedTaskCount` + `windowNotice` 如实说明"挡掉了多少"。
    renderedTaskCount: taskWindow.tasks.length,
    windowNotice: windowNotice(taskWindow),
    windowLimit: taskWindow.limit,
    toggleNodeCollapse,
    toggleDoneArchive,
    expandAll,
    collapseOneLayer,
    expandOneLayer,
    handleViewportMove,
    liveSpineEdgeIds,
    edges,
  };
}
