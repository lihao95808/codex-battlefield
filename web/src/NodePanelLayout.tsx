/**
 * 任务节点画布单元（`App.tsx` → `panels.tsx` → 这里，P5-2）。
 */
import { outcomePreviewForTask } from './model';
import { statusLabel } from './labels';
import { asRecord, asRecordArray, liveNodeStatuses, nodeBlockSummary, numberValue, taskEvidenceBadges, type TaskNodeData } from './taskView';
import { Archive, ChevronDown, ChevronRight, FileText } from 'lucide-react';
import { Handle, Position, type NodeProps } from '@xyflow/react';

export function TaskNodeView(props: NodeProps) {
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
        <span className={`status ${task.status}`}>{statusLabel(task.status)}</span>
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

export function channelLabel(raw: string): { label: string; detail: string } {
  const value = (raw ?? '').trim();
  if (!value || value === 'default') {
    return { label: '默认通道', detail: '使用默认 ~/.codex 配置' };
  }
  const parts = value.split(/[\\/]+/).filter(Boolean);
  return { label: parts.length ? parts[parts.length - 1] : value, detail: value };
}

export const nodeTypes = {
  task: TaskNodeView,
};
