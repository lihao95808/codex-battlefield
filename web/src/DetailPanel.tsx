/**
 * 节点详情面板（`App.tsx` → `panels.tsx` → 这里，P5-2）。
 *
 * 它按需拉产物（`fetchRunArtifact`），也负责展示阻塞/等待报告。
 */
import { fetchRunArtifact } from './api';
import { formatCostSummary, formatDateTime, formatUsageSummary } from './format';
import { blockParentActionLabel, blockRetryableLabel, eventTypeLabel, runKindLabel, runStatusLabel, statusLabel } from './labels';
import {
  constitutionPropagationHeadline,
  obligationBoardHeadline,
  obligationIndexHeadline,
  obligationIndexIsReadable,
  obligationIndexTypeBreakdown,
  obligationTypeBreakdown,
  versionDistributionEntries,
} from './model';
import { asRecord, asRecordArray, nodeBlockSummary, numberValue, stringList, type ArtifactKind } from './taskView';
import type {
  BattlefieldTask,
  ConstitutionBoard,
  ConstitutionResponse,
  ObligationBoard,
  ObligationIndexBoard,
  RunArtifactResponse,
  TaskDetailResponse,
} from './types';
import { BusyRunner, Section } from './uiPrimitives';
import { Activity, Ban, FileText, Pause, RefreshCw, RotateCcw, ShieldCheck, Terminal } from 'lucide-react';
import { useEffect, useState } from 'react';

export function DetailPanel({
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
        <span className={`status ${task.status}`}>{statusLabel(task.status)}</span>
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
                  {String(leaf.title ?? leaf.id)} · {statusLabel(String(leaf.status)) ?? String(leaf.status ?? '')}
                </span>
              ))}
            </div>
          )}
          {reportBlockedNodes.length > 0 && (
            <div className="report-list blocked">
              <strong>需要父层处理的节点</strong>
              {reportBlockedNodes.map((node) => (
                <span key={String(node.id)}>
                  {String(node.title ?? node.id)} · {String(node.reason ?? statusLabel(String(node.status)) ?? '')}
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
                <strong>{runKindLabel(run.kind) ?? run.kind}</strong>
                <span>{runStatusLabel(run.status) ?? run.status}</span>
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
              <strong>{eventTypeLabel(event.type) ?? event.type}</strong>
              <p>{event.message}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

export function ConstitutionPanel({
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

export function ObligationBoardPanel({ board }: { board: ObligationBoard | null }) {
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

export function ObligationIndexPanel({ board }: { board: ObligationIndexBoard | null }) {
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
