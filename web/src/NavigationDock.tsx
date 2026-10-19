/**
 * 导航控制台与画布数据（`App.tsx` → `panels.tsx` → 这里，P5-2）。
 *
 * 画布渲染约定（`nodeTypes` / `channelLabel` / `TreeStats` / `TaskNodeData`）不在这里 ——
 * 它们是常量与非组件函数，放在这里会让 Fast Refresh 失效（`react-refresh/only-export-components`），
 * 所以都在 `NodePanelLayout.tsx`。
 */
import { displaySlotCount, formatCostSummary, formatEventClock, formatUsageSummary, statNumber, statText } from './format';
import { eventSeverity, eventTypeLabel } from './labels';
import { type TreeStats } from './taskView';
import type { BattlefieldEvent, TreeUsage } from './types';
import { BusyRunner } from './uiPrimitives';
import { channelLabel, nodeTypes } from './NodePanelLayout';
import { Ban, ChevronsDown, ChevronsUp, Clock, Maximize2, Play, Terminal } from 'lucide-react';

export { nodeTypes };

export function EventTicker({ events }: { events: BattlefieldEvent[] }) {
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
            <span className="event-ticker-label">{eventTypeLabel(event.type) ?? event.type}</span>
            <span className="event-ticker-message">{event.message}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}


export function GlobalNavigationDock({
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
              {blocked.top}
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

