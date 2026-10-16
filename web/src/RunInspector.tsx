/**
 * 运行记录与产物查看器（P2-3）。
 *
 * ## 它解决什么
 *
 * 原先要回答"这一步**为什么**错了"，得手工去翻
 * `<state>/projects/<key>/runs/<node>/<phase>.jsonl` 和旁边的 `prompt.md` /
 * `last-message.json`。那些文件路径不短、格式不统一，而"看错误"恰恰是最高频的
 * 排障动作 —— 每次都要离开界面去开资源管理器，工具就没法自己闭环。
 *
 * ## 三个刻意的选择
 *
 * 1. **默认选中"最值得看"的那条 run**，而不是第一条：有失败就选失败，
 *    没有就选最近一条。用户点进来通常是因为"它不对"，让他自己去找失败那行
 *    是把排障成本推回给他。
 * 2. **产物按需拉取**（点 tab 才取）。事件流和 prompt 可能几百 KB，
 *    一次性全取会把时间浪费在用户不会看的文件上。
 * 3. **截断要显式说出来**：后端会截断大文件（`truncated`），界面必须标注，
 *    否则用户会把"看到的片段"当成全文，然后基于它下错结论。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchRunArtifact, fetchTask } from './api';
import type { BattlefieldRun, RunArtifactResponse, TaskDetailResponse } from './types';

/** 产物种类直接**取自**已有的响应类型，不再另立一个同义别名。 */
type RunArtifactKind = RunArtifactResponse['kind'];

/** run 状态 -> 语义色。失败必须比成功显眼（这条是排障面板的存在理由）。 */
function runTone(status: string): string {
  const value = status.toLowerCase();
  if (value.includes('fail') || value.includes('error')) return 'bad';
  if (value.includes('run') || value.includes('queue') || value.includes('pending')) return 'wait';
  if (value.includes('done') || value.includes('ok') || value.includes('complete')) return 'ok';
  return 'muted';
}

function bytesLabel(text: string): string {
  const bytes = new TextEncoder().encode(text).length;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function shortTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
}

const TABS: { kind: RunArtifactKind; label: string; hint: string }[] = [
  { kind: 'prompt', label: '提示词', hint: '发给模型的内容 —— 判断"它是不是被误导了"' },
  { kind: 'logs', label: '事件流', hint: 'codex 的原始事件 —— 失败原因通常在这' },
  { kind: 'last_message', label: '最后消息', hint: '这一轮最终说了什么' },
];

function ArtifactView({ runId }: { runId: number }) {
  const [kind, setKind] = useState<RunArtifactKind>('logs');
  const [data, setData] = useState<RunArtifactResponse | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    fetchRunArtifact(runId, kind)
      .then((payload) => {
        if (alive) setData(payload);
      })
      .catch((err: unknown) => {
        if (alive) {
          setData(null);
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId, kind]);

  const onCopy = useCallback(() => {
    if (data?.content) {
      void navigator.clipboard?.writeText(data.content);
    }
  }, [data]);

  return (
    <div className="ri-artifact">
      <div className="ri-tabs">
        {TABS.map((tab) => (
          <button
            key={tab.kind}
            type="button"
            className={`ri-tab${kind === tab.kind ? ' ri-tab-on' : ''}`}
            title={tab.hint}
            onClick={() => setKind(tab.kind)}
          >
            {tab.label}
          </button>
        ))}
        {data?.content && (
          <button type="button" className="secondary ri-copy" onClick={onCopy} title="复制全文">
            复制
          </button>
        )}
      </div>

      {loading && <p className="ri-hint">读取中…</p>}
      {error && <p className="ri-error">{error}</p>}

      {data && !loading && (
        <>
          <div className="ri-artifact-meta">
            <span>{data.path ?? '(没有这个产物)'}</span>
            {data.content && <span>{bytesLabel(data.content)}</span>}
          </div>
          {/* 截断必须说出来：不说的话用户会把片段当全文，然后基于它下错结论。 */}
          {data.truncated && (
            <p className="ri-warn">内容较大，这里只显示了一部分（后端已截断）。</p>
          )}
          {data.exists ? (
            <pre className="ri-pre">{data.content || '（空文件）'}</pre>
          ) : (
            <p className="ri-hint">这一轮没有产生该产物。</p>
          )}
        </>
      )}
    </div>
  );
}

export function RunInspector({ taskId }: { taskId: string | null }) {
  const [detail, setDetail] = useState<TaskDetailResponse | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!taskId) {
      setDetail(null);
      setSelectedRunId(null);
      return;
    }
    let alive = true;
    setLoading(true);
    setError('');
    fetchTask(taskId)
      .then((payload) => {
        if (!alive) return;
        setDetail(payload);
        // 默认选"最值得看"的那条：先失败，再最近。用户点进来通常是因为它不对。
        const runs = payload.runs ?? [];
        const failed = runs.find((run) => runTone(run.status) === 'bad' || run.exit_code);
        setSelectedRunId((failed ?? runs[runs.length - 1])?.id ?? null);
      })
      .catch((err: unknown) => {
        if (alive) {
          setDetail(null);
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [taskId]);

  const runs = useMemo(() => detail?.runs ?? [], [detail]);
  const events = useMemo(() => detail?.events ?? [], [detail]);

  if (!taskId) {
    return (
      <section className="run-inspector" aria-label="运行记录">
        <div className="ri-head">
          <h3>运行记录</h3>
        </div>
        <p className="ri-hint">选中一个节点，这里会列出它的每一轮运行与产物。</p>
      </section>
    );
  }

  return (
    <section className="run-inspector" aria-label="运行记录">
      <div className="ri-head">
        <h3>运行记录</h3>
        {detail?.task && (
          <span className="ri-task-title" title={detail.task.title}>
            {detail.task.title}
          </span>
        )}
      </div>

      {loading && <p className="ri-hint">读取中…</p>}
      {error && <p className="ri-error">{error}</p>}

      {detail?.task?.error_message && (
        <p className="ri-error">节点错误：{detail.task.error_message}</p>
      )}

      {!loading && runs.length === 0 && <p className="ri-hint">这个节点还没有运行记录。</p>}

      {runs.length > 0 && (
        <ul className="ri-runs">
          {runs.map((run: BattlefieldRun) => {
            const tone = runTone(run.status);
            return (
              <li key={run.id}>
                <button
                  type="button"
                  className={`ri-run${selectedRunId === run.id ? ' ri-run-on' : ''}`}
                  onClick={() => setSelectedRunId(run.id)}
                  title={run.command}
                >
                  <span className="ri-run-kind">{run.kind}</span>
                  <span className={`ri-run-status ri-tone-${tone}`}>
                    {run.status}
                    {run.exit_code !== null && run.exit_code !== undefined && run.exit_code !== 0
                      ? ` (exit ${run.exit_code})`
                      : ''}
                  </span>
                  <span className="ri-run-time">{shortTime(run.started_at)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {selectedRunId !== null && <ArtifactView runId={selectedRunId} />}

      {events.length > 0 && (
        <details className="ri-events">
          <summary>节点事件（{events.length}）</summary>
          <ul className="ri-event-list">
            {events.slice(-40).map((event) => (
              <li key={event.id}>
                <span className="ri-event-time">{shortTime(event.created_at)}</span>
                <code className="ri-event-type">{event.type}</code>
                <span className="ri-event-msg">{event.message}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
