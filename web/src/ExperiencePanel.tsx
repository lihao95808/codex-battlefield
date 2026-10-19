import { useEffect, useState } from 'react';

import { readDetail } from './api-http';
import {
  asObject, filterRecords, jsonText, knownText, learnedLabel, parseRecordsResponse,
  publicationStatus, recallEntries, recallIdentity, recordsUrl, recordStatusLabel,
  type JsonObject, type LearningRecord, type LearningRecordsResponse,
} from './experienceView';

function JsonDetails({ title, value }: { title: string; value: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="cc-preview" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{title}</summary>
      <pre className="cc-markdown" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        {jsonText(value)}
      </pre>
    </details>
  );
}

function StructuredSection({ title, value }: { title: string; value: unknown }) {
  const object = asObject(value);
  return (
    <section>
      <h4 className="cc-section-title">{title}</h4>
      {object && Object.keys(object).length > 0 ? (
        <dl style={{ margin: '0 0 8px', overflowWrap: 'anywhere' }}>
          {Object.entries(object).map(([key, item]) => (
            <div key={key} style={{ marginBottom: 6 }}>
              <dt><strong>{key}</strong></dt>
              <dd style={{ marginLeft: 12, whiteSpace: 'pre-wrap' }}>{jsonText(item)}</dd>
            </div>
          ))}
        </dl>
      ) : <p className="onb-hint">{object ? '没有记录条目（不代表已确认没有问题）。' : jsonText(value)}</p>}
    </section>
  );
}

function RecallEvidence({ payload }: { payload: JsonObject }) {
  const entries = recallEntries(payload);
  return (
    <section>
      <div className="onb-hint" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>召回查询：{jsonText(payload.query)}</div>
      <p className="onb-hint">召回内容仅为候选建议；命中、得分和来源哈希都不代表经验已经验证。</p>
      {asObject(payload.result)?.error != null && <p className="onb-error">召回错误：{jsonText(asObject(payload.result)?.error)}（结果不可用，不能据此判断没有经验。）</p>}
      {Array.isArray(payload.entries) && payload.entries.length === 0
        ? <p className="onb-hint">{payload.status === 'ok' ? '本次召回未返回条目（不代表没有相关经验）。' : '本次没有可用召回条目；请核对召回状态和原始结果。'}</p>
        : entries.length === 0 && <p className="onb-hint">召回条目未知或格式不可识别；请查看原始快照。</p>}
      {entries.map((entry, index) => {
        const identity = recallIdentity(entry);
        return (
          <div key={index} className="cc-row" style={{ overflowWrap: 'anywhere' }}>
            <div className="cc-section-title">候选建议 {index + 1}</div>
            <div>来源 URI：<code>{identity.uri}</code></div>
            <div>内容 SHA-256：<code>{identity.hash}</code></div>
            <div>召回得分：{knownText(entry.score)}（非验证分）</div>
            <JsonDetails title="当时召回的只读内容快照" value={entry.content} />
          </div>
        );
      })}
      <JsonDetails title="召回结果（原始 JSON）" value={payload.result} />
    </section>
  );
}

function RecordRow({ record }: { record: LearningRecord }) {
  const payload = record.payload;
  const context = asObject(payload.context);
  return (
    <li className="cc-row" style={{ overflowWrap: 'anywhere' }}>
      <div className="cc-row-head">
        <span className="cc-title">{record.kind === 'recall' ? '节点召回快照' : record.kind === 'review' ? '节点复盘 / 归档历史' : `未知记录类型（${record.kind}）`}</span>
        <span className="cc-status">{recordStatusLabel(record.status)}</span>
      </div>
      <div className="cc-row-meta">
        #{record.id} · 节点 {knownText(record.task_id)} · 阶段 {knownText(record.phase)}
        {' · '}尝试 {knownText(record.attempt_key)}
      </div>
      <div className="cc-row-meta">创建 {knownText(record.created_at)} · 更新 {knownText(record.updated_at)}</div>
      <div className="cc-row-meta">记录 SHA-256：<code>{knownText(record.content_sha256)}</code></div>
      <div className="cc-row-meta">OpenViking 任务：{knownText(record.provider_task_id)}</div>
      <p className="onb-hint">{learnedLabel(payload.learned)}</p>
      {record.kind === 'recall' && <RecallEvidence payload={payload} />}
      {record.kind === 'review' && (
        <>
          <StructuredSection title="集成事实（记录的事实；不等同于经验验证）" value={payload.facts} />
          <StructuredSection title="复盘分析 / 假设（非事实、非已验证经验）" value={payload.analysis} />
          <h4 className="cc-section-title">复盘当时使用的召回上下文</h4>
          {context ? (
            <>
              <p className="onb-hint">{recordStatusLabel(context.status)}</p>
              <RecallEvidence payload={context} />
            </>
          ) : <p className="onb-hint">上下文未知（未提供）。</p>}
          <p className="onb-hint">归档状态：{recordStatusLabel(publicationStatus(payload))}</p>
          <p className="onb-hint">归档被接受或提供方任务 completed 只表示处理进度，不代表经验被验证或已经学习。</p>
          {asObject(payload.publication)?.error != null && <p className="onb-error">归档错误：{jsonText(asObject(payload.publication)?.error)}</p>}
          <StructuredSection title="提供方归档响应 / 处理结果（不代表已学习）" value={payload.publication} />
        </>
      )}
      <JsonDetails title="完整只读记录快照（原始 JSON）" value={record} />
    </li>
  );
}

type LoadState = {
  taskId: string | null;
  busy: boolean;
  data: LearningRecordsResponse | null;
  error: string;
};

/** Mount in 日志与诊断. No requests until this disclosure is opened. */
export function ExperiencePanel({ taskId }: { taskId: string | null }) {
  const [open, setOpen] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('');
  const [status, setStatus] = useState('');
  const [state, setState] = useState<LoadState>({ taskId, busy: false, data: null, error: '' });

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setState({ taskId, busy: true, data: null, error: '' });
    void (async () => {
      try {
        const response = await fetch(recordsUrl(taskId), { signal: controller.signal });
        if (!response.ok) throw new Error(await readDetail(response, '读取记忆记录失败'));
        const data = parseRecordsResponse(await response.json());
        if (!controller.signal.aborted) setState({ taskId, busy: false, data, error: '' });
      } catch (error: unknown) {
        if (!controller.signal.aborted) {
          setState({ taskId, busy: false, data: null, error: error instanceof Error ? error.message : String(error) });
        }
      }
    })();
    return () => controller.abort();
  }, [open, taskId, refresh]);

  const current = state.taskId === taskId ? state : null;
  const data = current?.data;
  const records = data ? filterRecords(data.records, query, kind, status) : [];
  const statuses = [...new Set(data?.records.map((record) => record.status).filter((value): value is string => typeof value === 'string' && !!value) ?? [])];

  return (
    <details className="conclusion-panel" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>经验与外部记忆（只读）{data && <span className="cc-count">{data.records.length}</span>}</summary>
      {open && (
        <>
          <p className="onb-hint">OpenViking 外部记忆的召回与复盘审计。候选建议不是已验证经验，归档接受不是学习成功。</p>
          <p className="onb-hint">{taskId ? `当前节点：${taskId}` : '未选择节点：查询全部记录。'}</p>
          <div className="onb-actions">
            <button type="button" className="secondary" disabled={current?.busy} onClick={() => setRefresh((value) => value + 1)}>
              {current?.busy ? '读取中…' : '刷新记录'}
            </button>
          </div>
          {(!current || current.busy) && <p className="onb-hint" role="status">正在读取记忆记录…</p>}
          {current?.error && <p className="onb-error" role="alert">{current.error}（未能读取记录，不能据此判断为空。）</p>}
          {data && (
            <>
              <p className="onb-hint">
                提供方：{data.provider.backend} · {data.provider.enabled ? '已启用' : '未启用'}
                {' · '}{data.provider.configured ? '已配置（不代表连接健康）' : '未配置'}
              </p>
              <label className="onb-field">
                <span className="onb-label">搜索已读取记录（节点、URI、事实、分析）</span>
                <input className="onb-input" value={query} onChange={(event) => setQuery(event.target.value)} />
              </label>
              <label className="onb-field">
                <span className="onb-label">记录类型</span>
                <select className="cfg-select" value={kind} onChange={(event) => setKind(event.target.value)}>
                  <option value="">全部类型</option><option value="recall">召回</option><option value="review">复盘 / 归档</option>
                </select>
              </label>
              <label className="onb-field">
                <span className="onb-label">处理状态（不代表经验验证状态）</span>
                <select className="cfg-select" value={status} onChange={(event) => setStatus(event.target.value)}>
                  <option value="">全部状态</option>
                  {status && !statuses.includes(status) && <option value={status}>{recordStatusLabel(status)}</option>}
                  {statuses.map((value) => <option key={value} value={value}>{recordStatusLabel(value)}</option>)}
                </select>
              </label>
              {data.records.length === 0 ? <p className="onb-hint">读取成功：当前范围尚无记忆审计记录。</p>
                : records.length === 0 ? <p className="onb-hint">已有 {data.records.length} 条记录，但没有符合当前筛选的记录。</p>
                : <ul className="cc-list">{records.map((record) => <RecordRow key={record.id} record={record} />)}</ul>}
            </>
          )}
        </>
      )}
    </details>
  );
}
