/** 左栏与主工作区（P5-2：从 `App.tsx` 的 `return` 里按区域抽出）。
 *
 * 为什么按"区域"抽而不是按 props 逐个传：这两块用到 `view` 的六七十个字段，
 * 逐个传等于把 props 类型再抄一遍（几百行），而 JSX 本身已写成 `view.xxx`。
 * 所以它们吃**一个** `view` prop —— 改动最小、也最容易看出"没改行为"。
 */
import { useBattlefieldController } from './useBattlefieldController';
import { RailDisclosure } from './RailDisclosure';
import { RailAttention } from './RailAttention';

type View = ReturnType<typeof useBattlefieldController>;
import { Stat } from './uiPrimitives';
import { WatchdogPanel } from './WatchdogPanel';
import { EventTicker } from './NavigationDock';
import { ConstitutionPanel, ObligationBoardPanel, ObligationIndexPanel } from './DetailPanel';
import { Archive, ChevronsDown, ChevronsUp, FileText, Layers, Maximize2, Play, RefreshCw, Search, ShieldCheck, Zap } from 'lucide-react';
import { autoConcurrencyLabel, statusLabels } from './labels';
import { statNumber, statText } from './format';
import { ProjectOnboarding } from './ProjectOnboarding';
import { CostPanel } from './CostPanel';
import { PolicyPanel } from './PolicyPanel';
import { PreAuthPanel } from './PreAuthPanel';
import { ConclusionPanel } from './ConclusionPanel';
import { CoordinationPanel } from './CoordinationPanel';
import { DeliveryPanel } from './DeliveryPanel';
import { MetricsPanel } from './MetricsPanel';
import { CrossProjectPanel } from './CrossProjectPanel';
import { GlossaryPanel } from './GlossaryPanel';
import { LanguageSwitch } from './LanguageSwitch';
import { RunInspector } from './RunInspector';
import { ExperiencePanel } from './ExperiencePanel';
import { SettingsPanel } from './SettingsPanel';
import { StateAdminPanel } from './StateAdminPanel';
import { TaskHierarchy } from './TaskHierarchy';
import { constitutionBoardFromStats, obligationBoardFromStats } from './model';
import type { TaskStatus } from './types';

export function LeftRail({ view }: { view: View }) {
  return (
    <>
      <aside className="left-rail">
          <div className="brand">
            <div className="brand-mark">
              <Zap size={22} />
            </div>
            <div>
              <h1>任务战场</h1>
              <p>AI L4 自动导航任务可视化</p>
            </div>
            <LanguageSwitch />
          </div>
  
          <TaskHierarchy tasks={view.treeIsStale ? [] : view.tree?.tasks ?? []} selectedId={view.selectedId}
            collapsedIds={view.collapsedNodeIds} depthLimit={view.visibleDepthLimit} onSelect={view.setSelectedId}
            onToggle={view.toggleNodeCollapse} onExpandAll={view.expandAll} onCollapse={view.collapseOneLayer} />
          <RailDisclosure title="新建任务" hint="目标与边界">
          <section className="panel">
            <label htmlFor="goal">导航目标</label>
            <textarea id="goal" value={view.goal} onChange={(event) => view.setGoal(event.target.value)} />
            <div className="rail-mode-note">运行方式：{view.runnerMode === 'real' ? '真实 Codex' : '模拟'}</div>
            <RailDisclosure title="运行选项" hint="路径与运行方式">
            <label htmlFor="targetPath">目标路径（高级模式）</label>
            <input
              id="targetPath"
              value={view.targetPath}
              onChange={(event) => view.setTargetPath(event.target.value)}
              placeholder="可选：Git 仓库或工作区路径"
            />
            <div className="mode-toggle" role="group" aria-label="运行模式">
              <button className={view.runnerMode === 'mock' ? 'active' : ''} onClick={() => view.setRunnerMode('mock')} type="button">
                模拟
              </button>
              <button className={view.runnerMode === 'real' ? 'active' : ''} onClick={() => view.setRunnerMode('real')} type="button">
                真实 Codex
              </button>
            </div>
            </RailDisclosure>
            <p className="input-hint">写下目标 → 确认边界 → 启动任务。</p>
          </section>
  
          <section className="panel constitution-editor" aria-label="项目宪法编辑">
            <div className="panel-row">
              <label htmlFor="constitutionMarkdown">任务边界（项目宪法）</label>
              <span className={`constitution-chip ${view.activeConstitution?.status ?? 'empty'}`}>
                {view.constitutionLocked ? '已锁定' : view.activeConstitution ? '草案' : '未生成'}
              </span>
            </div>
            <div className="constitution-actions">
              <button className="secondary" onClick={view.onDraftConstitution} disabled={view.busy || view.goal.trim().length === 0} type="button" title="生成宪法草案">
                <FileText size={15} />
                生成草案
              </button>

            </div>
            {view.activeConstitution ? (
              <>
                <textarea
                  id="constitutionMarkdown"
                  className="constitution-markdown"
                  value={view.constitutionMarkdown}
                  onChange={(event) => view.setConstitutionMarkdown(event.target.value)}
                  disabled={!view.canEditConstitution}
                />
                <RailDisclosure title="编辑结构化边界" hint="高级修订">
                <label htmlFor="constitutionData">结构化边界</label>
                <textarea
                  id="constitutionData"
                  className="constitution-data"
                  value={view.constitutionDataText}
                  onChange={(event) => view.setConstitutionDataText(event.target.value)}
                  disabled={!view.canEditConstitution}
                />
                </RailDisclosure>
                <button className="secondary" onClick={view.onSaveConstitution} disabled={view.busy || !view.canEditConstitution} type="button" title="保存宪法修订">
                  <RefreshCw size={15} />保存修订
                </button>
                <div className="constitution-actions">
                  <button className="secondary" onClick={view.onLockConstitution} disabled={view.busy || !view.canEditConstitution} type="button" title="锁定项目宪法">
                    <ShieldCheck size={15} />
                    锁定宪法
                  </button>
                  <button
                    className="primary"
                    onClick={view.onStart}
                    disabled={view.busy || !view.constitutionLocked || !view.constitutionMatchesInput}
                    type="button"
                    title="启动递归任务树"
                  >
                    <Play size={17} />
                    启动任务树
                  </button>
                </div>
                <code className="constitution-hash">
                  v{view.activeConstitution.version} · {view.activeConstitution.content_hash}
                </code>
              </>
            ) : null}
          </section>
  
          </RailDisclosure>
          <RailDisclosure title="画布显示" hint="筛选与层级">
          <section className="panel">
            <label htmlFor="filter">视图</label>
            <div className="select-wrap">
              <Search size={16} />
              <select id="filter" value={view.statusFilter} onChange={(event) => view.setStatusFilter(event.target.value as TaskStatus | 'all' | 'active')}>
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
                onClick={() => view.setExpandedDoneArchiveParentIds(new Set())}
                type="button"
                title="完成历史按父节点局部展开；点击这里收回已经展开的历史分支，避免长跑画布卡顿。"
              >
                <Archive size={15} />
                轻量历史保护
              </button>
              <span>
                已压缩 {statNumber(view.tree?.stats, 'archived_done_nodes', view.doneArchive.hiddenIds.size)} 个完成节点；点节点上的归档数字局部展开
              </span>
            </div>
          </section>
  
          <section className="panel">
            <div className="panel-row">
              <label>显示深度</label>
              <button className="ghost-action" onClick={view.expandAll} type="button" title="展开全部">
                <Maximize2 size={15} />
              </button>
            </div>
            <div className="depth-stepper" aria-label="逐层展开折叠">
              <button onClick={view.collapseOneLayer} disabled={view.treeIsStale || view.effectiveDepthLimit <= 0} type="button" title="向上收起一层">
                <ChevronsUp size={16} />
              </button>
              <span>{view.depthDisplayLabel}</span>
              <button onClick={view.expandOneLayer} disabled={view.treeIsStale || view.visibleDepthLimit === null || view.effectiveDepthLimit >= view.maxDepth} type="button" title="向下展开一层">
                <ChevronsDown size={16} />
              </button>
            </div>
            <div className="depth-controls" aria-label="自上而下显示深度控制">
              {view.availableDepths.map((depth) => (
                <button
                  key={depth}
                  className={view.visibleDepthLimit === depth ? 'active' : ''}
                  onClick={() => view.setVisibleDepthLimit(depth)}
                  type="button"
                  title={depth === 0 ? '只显示根层' : `显示到第 ${depth} 层`}
                >
                  <Layers size={14} />
                  {depth === 0 ? '根层' : `到 ${depth} 层`}
                </button>
              ))}
              <button className={view.visibleDepthLimit === null ? 'active' : ''} onClick={view.expandAll} type="button" title="显示全部层级">
                <Maximize2 size={14} />
                全部
              </button>
            </div>

          </section>
  
          </RailDisclosure>
          {/* 省略窗口的告知始终可见，不能跟着显示设置一起藏起来。 */}
          {view.windowNotice && <p className="depth-note" data-testid="virtualization-notice">
            画布渲染 {view.renderedTaskCount} 个节点。{view.windowNotice}
          </p>}
          <section className="stat-grid" aria-label="战场统计主项" data-testid="stat-grid-primary">
            <Stat label="总数" value={statNumber(view.visibleStats, 'total')} />
            <Stat label="完成" value={statNumber(view.visibleStats, 'done')} />
            <Stat
              label="Codex 槽位"
              value={`${view.visibleActiveSlots}/${view.visibleSlotCapacity}`}
              title="真实正在占用 Codex CLI 的并发槽位。"
            />

          </section>
  
          <details className="stat-more">
            <summary>更多指标（模型、预算、状态细分）</summary>
            <section className="stat-grid" aria-label="战场统计其余项">
              <Stat label="运行节点" value={statNumber(view.visibleStats, 'active_agent_statuses', statNumber(view.visibleStats, 'active_agents'))} />
              <Stat label="并发上限" value={statNumber(view.visibleStats, 'max_concurrency', 1)} />
              <Stat label="自动油门" value={autoConcurrencyLabel(statText(view.visibleStats, 'auto_concurrency_mode', 'pending'))} title={statText(view.visibleStats, 'auto_concurrency_reason', '等待下一次调度评估。')} />
              <Stat label="Codex 模型" value={statText(view.visibleStats, 'codex_model')} />
              <Stat label="推理档位" value={statText(view.visibleStats, 'codex_reasoning_effort')} />
              <Stat label="Goal 模式" value={statNumber(view.visibleStats, 'goal_mode') ? '长跑' : '保守'} />
              <Stat label="重试次数" value={statNumber(view.visibleStats, 'codex_retry_attempts')} />
              <Stat
                label="启动预算"
                value={
                  statNumber(view.visibleStats, 'agent_burst_limit') > 0
                    ? `${statNumber(view.visibleStats, 'agent_burst_used')}/${statNumber(
                        view.visibleStats,
                        'agent_burst_work_limit',
                        statNumber(view.visibleStats, 'agent_burst_limit'),
                      )}`
                    : '不限'
                }
              />
              <Stat label="自动保留" value={statNumber(view.visibleStats, 'agent_burst_reserve')} />
              <Stat label="剩余工作" value={statNumber(view.visibleStats, 'agent_burst_remaining')} />
              <Stat label="预算自启" value={`${Math.round(statNumber(view.visibleStats, 'agent_burst_resume_after_seconds') / 3600)} 小时`} />
              <Stat label="硬超时" value={statNumber(view.visibleStats, 'codex_timeout_seconds')} />
              <Stat label="空闲看门狗" value={statNumber(view.visibleStats, 'codex_idle_timeout_seconds')} />
              <Stat label="Godot 超时" value={statNumber(view.visibleStats, 'godot_timeout_seconds')} />
              <Stat label="自动休眠" value={statNumber(view.visibleStats, 'auto_hibernate_on_resource_block') ? '开启' : '关闭'} />
              <Stat label="自启排程" value={statNumber(view.visibleStats, 'scheduled_auto_resumes')} />
              <Stat label="接力延迟" value={`${statNumber(view.visibleStats, 'scheduler_deferred_retry_seconds')} 秒`} />
              <Stat label="待接力" value={statNumber(view.visibleStats, 'scheduled_deferred_resumes')} />
              <Stat label="排队中" value={statNumber(view.visibleStats, 'queued')} />
              <Stat label="等待父节点" value={statNumber(view.visibleStats, 'waiting_children')} />
              <Stat label="执行中" value={statNumber(view.visibleStats, 'working', statNumber(view.visibleStats, 'running'))} />
              <Stat label="验收中" value={statNumber(view.visibleStats, 'verifying')} />
              <Stat label="休眠中" value={statNumber(view.visibleStats, 'hibernating')} />
              <Stat label="阻塞" value={statNumber(view.visibleStats, 'blocked')} />
              <Stat label="有效阻塞" value={statNumber(view.visibleStats, 'blocked_active')} />
              <Stat label="额度等待" value={statNumber(view.visibleStats, 'blocked_resource_wait')} />
              <Stat label="修复中阻塞" value={statNumber(view.visibleStats, 'blocked_under_repair')} />
              <Stat label="停用分支阻塞" value={statNumber(view.visibleStats, 'blocked_inactive_branch')} />
              <Stat label="前线" value={statNumber(view.visibleStats, 'active_frontier')} />
            </section>
          </details>
  
          <RailDisclosure title="工程绑定" hint={view.layoutState?.bound ? '已绑定 · 切换工程' : '未绑定 · 可选'}
            keepMounted forceOpen={Boolean(view.layoutState && !view.layoutState.bound)}>
            <ProjectOnboarding onBound={() => view.loadTree().catch(() => undefined)} />
          </RailDisclosure>
          <RailAttention layout={view.layoutState} />
          <div className="rail-tools" aria-label="更多工具">
            <RailDisclosure title="工程与设置" hint="配置、数据维护">
              <SettingsPanel />
              <StateAdminPanel />
              <GlossaryPanel />
            </RailDisclosure>
            <RailDisclosure title="管家与授权" hint="对话、审批、看门狗">
              <PreAuthPanel />
              <CoordinationPanel />
              <WatchdogPanel
                state={view.watchdogState} layout={view.layoutState} busy={view.busy}
                result={view.watchdogResult} onInstall={view.onInstallWatchdog}
                onRunOnce={view.onRunWatchdogOnce} onToggleDrain={view.onToggleApiRestartDrain}
              />
              <PolicyPanel />
            </RailDisclosure>
            <RailDisclosure title="进度与成本" hint="投递、费用、跨工程">
              <DeliveryPanel />
              <CostPanel />
              <CrossProjectPanel />
              <ConclusionPanel />
            </RailDisclosure>
            <RailDisclosure title="日志与诊断" hint="运行记录、指标、约束">
              <ExperiencePanel taskId={view.selectedId} />
              <RunInspector taskId={view.selectedId} />
              <MetricsPanel />
              <ConstitutionPanel constitution={view.activeConstitution}
                board={constitutionBoardFromStats(view.treeIsStale ? undefined : view.tree?.stats)} />
              <ObligationBoardPanel board={obligationBoardFromStats(view.treeIsStale ? undefined : view.tree?.stats)} />
              <ObligationIndexPanel board={view.obligationIndex} />
              <EventTicker events={view.treeIsStale ? [] : (view.tree?.events ?? [])} />
            </RailDisclosure>
          </div>
  
          {view.error && <div className="error">{view.error}</div>}
        </aside>
    </>
  );
}
