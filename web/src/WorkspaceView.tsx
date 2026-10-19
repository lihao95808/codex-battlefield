/** 左栏与主工作区（P5-2：从 `App.tsx` 的 `return` 里按区域抽出）。
 *
 * 为什么按"区域"抽而不是按 props 逐个传：这两块用到 `view` 的六七十个字段，
 * 逐个传等于把 props 类型再抄一遍（几百行），而 JSX 本身已写成 `view.xxx`。
 * 所以它们吃**一个** `view` prop —— 改动最小、也最容易看出"没改行为"。
 */
import { useBattlefieldController } from './useBattlefieldController';

type View = ReturnType<typeof useBattlefieldController>;
import { nodeLodForZoom, TaskNodeData } from './taskView';
import { GlobalNavigationDock, nodeTypes } from './NavigationDock';
import { DetailPanel } from './DetailPanel';
import { Background, Controls, MiniMap, ReactFlow } from '@xyflow/react';
import { ShieldCheck } from 'lucide-react';
import { statNumber } from './format';
import { StewardPanel } from './StewardPanel';

export function WorkspaceView({ view }: { view: View }) {
  return (
    <>
        <div className="center-workspace">
        <section className="battlefield" aria-label="L4 自动导航画布">
          {view.treeIsStale ? (
            <div className="tree-empty-state">
              <ShieldCheck size={28} />
              <h3>导航边界已生成，等待启航</h3>
              <p>当前画布已隐藏上一条旧航线。请先锁定这份边界，再启动导航；启动后这里会显示递归拆解与完成路径。</p>
              <code>
                当前宪法 {view.activeConstitution?.content_hash ?? '未生成'} · 旧树宪法 {view.treeConstitution?.content_hash ?? '无'}
              </code>
            </div>
          ) : (
            <ReactFlow
              className={`tree-flow lod-${view.nodeLod}`}
              nodes={view.nodes}
              edges={view.edges}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{
                nodes: [...view.nodes].sort((a, b) => a.position.y - b.position.y).slice(0, 6).map(({ id }) => ({ id })),
                padding: 0.15,
                maxZoom: 0.85,
              }}
              minZoom={0.15}
              maxZoom={1.5}
              elevateEdgesOnSelect
              onInit={(instance) => view.setNodeLod(nodeLodForZoom(instance.getZoom()))}
              onMove={view.handleViewportMove}
              onNodeClick={(_, node) => view.setSelectedId(node.id)}
              onNodeDoubleClick={(_, node) => {
                const task = (node.data as TaskNodeData).task;
                if (view.parentIds.has(task.id)) {
                  view.toggleNodeCollapse(task);
                }
              }}
            >
              <Background gap={24} size={1} />
              <MiniMap pannable zoomable nodeStrokeWidth={3} />
              <Controls />
            </ReactFlow>
          )}
        </section>
        <section className="chat-dock" aria-label="底部聊天面板">
          <StewardPanel
            layout={view.layoutState}
            onOpen={() => view.loadTree().catch(() => undefined)}
            navigation={<GlobalNavigationDock
            stats={view.visibleStats}
            usage={view.treeIsStale ? null : (view.tree?.usage ?? null)}
            blocked={view.blockedSummary}
            visibleNodeCount={view.visibleNodeCount}
            maxDepth={statNumber(view.visibleStats, 'max_depth')}
            busy={view.busy}
            busyLabel={view.busyLabel}
            canStart={view.goal.trim().length > 0}
            canControl={!!view.tree?.tasks.length && view.hasActiveFlow}
            canHibernate={!!view.activeRootId && view.hasActiveFlow}
            treeIsStale={view.treeIsStale}
            effectiveDepthLimit={view.effectiveDepthLimit}
            visibleDepthLimit={view.visibleDepthLimit}
            maxDepthValue={view.maxDepth}
            onStart={view.onOneShotStart}
            onHibernate={view.onHibernateRoot}
            onCancel={view.onCancelAll}
            onCollapseOneLayer={view.collapseOneLayer}
            onExpandOneLayer={view.expandOneLayer}
            onExpandAll={view.expandAll}
            />}
          />
        </section>
        </div>
        <aside className="right-rail node-inspector" aria-label="选中节点详情">
          <header className="node-inspector-head"><h2>节点详情</h2></header>
          <div className="node-inspector-scroll">
          <DetailPanel
            detail={view.treeIsStale ? null : view.detail}
            waitingReport={view.detail?.task.id ? (view.waitingReports.get(view.detail.task.id) ?? null) : null}
            taskById={view.taskById}
            busy={view.busy}
            busyLabel={view.busyLabel}
            onAction={view.onAction}
          />
          </div>
        </aside>
    </>
  );
}
