import { Ban, Pause, RefreshCw, RotateCcw } from 'lucide-react';
import { formatCostSummary, formatUsageSummary } from './format';
import { completionReport, taskImageArtifacts } from './nodeInspection';
import type { TaskDetailResponse } from './types';
import { ImagePreview } from './NodeActivity';

/**
 * 已完成节点的详情：只回答“干了什么、交付了什么”。
 *
 * 用户明确要求完成态不回放过程，所以这里不渲染运行日志与提示词；
 * 需要过程时退回未完成节点的过程视图（或在树里选中仍在运行的节点）。
 */
export function NodeCompletion({ detail, busy, onAction }: {
  detail: TaskDetailResponse;
  busy: boolean;
  onAction: (action: 'retry' | 'pause' | 'resume' | 'cancel') => void;
}) {
  const { task } = detail;
  const report = completionReport(task, detail.runs);
  const images = taskImageArtifacts(task);
  return (
    <div className="detail node-completion" aria-label="节点完成汇报">
      <div className="detail-head">
        <span className="status done">已完成</span>
        {task.is_active_frontier && <span className="frontier-pill">当前前线</span>}
      </div>
      <h2>{task.title}</h2>
      <dl className="node-basic-info">
        <dt>节点 ID</dt><dd>{task.id}</dd>
        <dt>任务深度</dt><dd>{task.depth}</dd>
        <dt>父节点</dt><dd>{task.parent_id ?? '根节点'}</dd>
        <dt>工作目录</dt><dd>{task.worktree_path ?? '尚未创建'}</dd>
      </dl>

      <div className="actions">
        <button onClick={() => onAction('retry')} disabled={busy} title="重试此任务"><RotateCcw size={16} /></button>
        <button onClick={() => onAction('pause')} disabled={busy} title="暂停此任务"><Pause size={16} /></button>
        <button onClick={() => onAction('resume')} disabled={busy} title="继续此任务"><RefreshCw size={16} /></button>
        <button onClick={() => onAction('cancel')} disabled={busy} title="取消此任务"><Ban size={16} /></button>
      </div>

      <section className="detail-section">
        <h3>完成了什么</h3>
        {report.summary
          ? <p className="node-completion-summary">{report.summary}</p>
          : <p className="muted">节点已标记完成，但没有记录可读的完成汇报 —— 不能仅凭状态推断它做了什么。</p>}
      </section>

      {images.length > 0 && (
        <section className="detail-section" aria-label="美术产出预览">
          <h3>交付缩略图 · {images.length}</h3>
          <div className="node-art-grid">
            {images.map((image) => <ImagePreview key={image.path} image={image} />)}
          </div>
        </section>
      )}

      {report.evidence.length > 0 && (
        <section className="detail-section">
          <h3>完成证据 · {report.evidence.length}</h3>
          <ul className="node-completion-list">
            {report.evidence.slice(0, 4).map((text) => <li key={text}>{text}</li>)}
          </ul>
          {report.evidence.length > 4 && (
            <details><summary>其余 {report.evidence.length - 4} 项证据</summary>
              <ul className="node-completion-list">{report.evidence.slice(4).map((text) => <li key={text}>{text}</li>)}</ul>
            </details>
          )}
        </section>
      )}

      {report.files.length > 0 && (
        <section className="detail-section">
          <h3>交付文件 · {report.files.length}</h3>
          <ul className="node-completion-list">
            {report.files.slice(0, 6).map((file) => <li key={file}><code>{file}</code></li>)}
          </ul>
          {report.files.length > 6 && (
            <details><summary>全部交付文件</summary><pre className="json-block">{report.files.join('\n')}</pre></details>
          )}
        </section>
      )}

      {!images.length && <p className="muted">没有已记录的图片产出，以文字汇报为准。</p>}
      <p className="muted">
        {formatUsageSummary(detail.usage) ?? '该节点没有带回用量的运行记录。'} {formatCostSummary(detail.usage) ?? ''}
      </p>
    </div>
  );
}
