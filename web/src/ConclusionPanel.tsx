/**
 * 结论文档面板（P4-5）。
 *
 * ## 为什么需要它
 *
 * 事件表记的是**发生了什么**，它答不了"**当时为什么这么决定**"。而后者恰恰是
 * 复盘时唯一真正需要的东西 —— 事后看到一条 `cancel_node`，光看事件永远不知道
 * 那是不是个合理决定。
 *
 * 所以这个面板做两件事：代码把**事实**抽出来（影响面 / 决策点 / 代价 / 未清债），
 * 你把"当时为什么"写上去。
 *
 * ## 三条显示规矩（由 `conclusionView.ts` 的纯函数 + 测试钉住）
 *
 * 1. **草稿要显眼标出"没有落库"** —— 否则用户以为已经归档了，关掉页面就没了。
 * 2. **"没能确定的地方"必须显示**（含"没有未确定项"这句话本身）——
 *    留空会被读成"确定没有"。
 * 3. **`authored` 为空时不许提交** —— 只有事实的文档只是另一份事件表。
 */
import { useCallback, useEffect, useState } from 'react';

import { conclusionExportUrl, fetchConclusionDraft, fetchConclusions, saveConclusion } from './api';
import {
  conclusionStatusLabel,
  draftStatusText,
  exportFileName,
  incompleteText,
  validateConclusion,
} from './conclusionView';
import type { ConclusionDraft, ConclusionPayload } from './types';

export function ConclusionPanel() {
  const [draft, setDraft] = useState<ConclusionDraft | null>(null);
  const [saved, setSaved] = useState<ConclusionPayload[]>([]);
  const [authored, setAuthored] = useState('');
  const [author, setAuthor] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const loadList = useCallback(() => {
    fetchConclusions()
      .then((payload) => setSaved(payload.conclusions))
      .catch(() => setSaved([]));
  }, []);

  useEffect(() => {
    loadList();
  }, [loadList]);

  const makeDraft = () => {
    setBusy(true);
    setError('');
    setNotice('');
    fetchConclusionDraft()
      .then(setDraft)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setBusy(false));
  };

  const submit = async () => {
    if (!draft) return;
    const problem = validateConclusion(authored);
    if (problem.field) {
      setError(problem.message);
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const row = await saveConclusion({
        rootId: draft.root_id,
        authored,
        title: draft.title,
        nodeId: draft.node_id,
        author,
        facts: draft.facts,
        incomplete: draft.incomplete,
      });
      setNotice(`已归档为 #${row.id}（${conclusionStatusLabel(row.status)}）。`);
      setAuthored('');
      setDraft(null);
      loadList();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="conclusion-panel">
      <summary>
        结论文档
        {saved.length > 0 && <span className="cc-count">{saved.length}</span>}
      </summary>

      <p className="onb-hint">
        事件表答不了"当时为什么这么决定"，而复盘要的正是那句话。
        代码抽事实，你补判断 —— 两段在文档里严格分开。
      </p>

      {error && <p className="onb-error">{error}</p>}
      {notice && <p className="onb-notice">{notice}</p>}

      <div className="onb-actions">
        <button type="button" className="secondary" disabled={busy} onClick={makeDraft}>
          {busy ? '生成中…' : draft ? '重新生成草稿' : '生成草稿（不落库）'}
        </button>
      </div>

      {draft && (
        <div className="cc-draft">
          <div className="cc-draft-status">{draftStatusText(draft)}</div>

          {/* 事实与"没能确定"都要显示：空段被读成"确定没有"是最坏的一种误读。 */}
          <div className="cc-incomplete">
            <div className="cc-section-title">这份草稿里没能确定的地方</div>
            <ul>
              {incompleteText(draft).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>

          <label className="onb-field">
            <span className="onb-label">当时为什么这么决定（必填）</span>
            <textarea
              className="cc-textarea"
              value={authored}
              disabled={busy}
              rows={6}
              placeholder="当时基于什么信息做了这个决定？现在看，哪一步的判断是错的？"
              onChange={(event) => setAuthored(event.target.value)}
            />
          </label>
          <label className="onb-field">
            <span className="onb-label">署名（可选）</span>
            <input
              className="onb-input"
              value={author}
              spellCheck={false}
              disabled={busy}
              onChange={(event) => setAuthor(event.target.value)}
            />
          </label>

          <div className="onb-actions">
            <button
              type="button"
              className="onb-bind"
              disabled={busy || !authored.trim()}
              onClick={() => void submit()}
            >
              {busy ? '归档中…' : '归档这份结论'}
            </button>
          </div>

          <details className="cc-preview">
            <summary>预览（代码抽的部分）</summary>
            <pre className="cc-markdown">{draft.markdown}</pre>
          </details>
        </div>
      )}

      {saved.length > 0 && (
        <ul className="cc-list">
          {saved.map((item) => (
            <li key={item.id} className="cc-row">
              <div className="cc-row-head">
                <span className="cc-title">{item.title}</span>
                <span className={`cc-status cc-status-${item.status}`}>
                  {conclusionStatusLabel(item.status)}
                </span>
              </div>
              <div className="cc-row-meta">
                #{item.id} · {item.root_id}
                {item.author ? ` · ${item.author}` : ''}
              </div>
              <a className="cc-export" href={conclusionExportUrl(item.id)} download={exportFileName(item)}>
                导出 {exportFileName(item)}
              </a>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
