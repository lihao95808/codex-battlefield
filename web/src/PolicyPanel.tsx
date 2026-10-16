/**
 * 自动策略面板（P3-5）。
 *
 * ## 为什么需要它
 *
 * 系统里本来就有"连续失败别重试了""没活干就休眠"这类动作，但它们散在各处，
 * **没有任何地方能回答**：现在有哪几条？各自开着吗？上次为什么没触发？
 *
 * 最后那个问题最要紧：没有这个面板时，"策略关掉了"与"策略没到条件"看起来
 * 完全一样。前者意味着**你以为有保护其实没有** —— 而你不会去查。
 *
 * ## 三态显示（由 `policyView.ts` 的纯函数 + 测试钉住）
 *
 * | 状态 | 用户该做什么 |
 * |---|---|
 * | 已关闭 | 去设置里打开 |
 * | 已触发 | 看依据，确认动作对不对 |
 * | 守护中 | 什么都不用做 |
 *
 * 把"已关闭"混进"守护中"是这个面板唯一会犯的严重错误，所以标题徽标会优先报关闭数。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchAutoPolicy } from './api';
import { STATUS_LABEL, policyBadge, policyRows, policySummary } from './policyView';
import type { AutoPolicyReport } from './types';

export function PolicyPanel() {
  const [data, setData] = useState<AutoPolicyReport | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    fetchAutoPolicy()
      .then((payload) => {
        setData(payload);
        setError('');
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const rows = useMemo(() => (data ? policyRows(data) : []), [data]);

  return (
    <details className="policy-panel" open={(data?.fired_count ?? 0) > 0 || (data?.disabled.length ?? 0) > 0}>
      <summary>
        自动策略
        {data && <span className={`pg-badge pg-badge-${data.fired_count > 0 ? 'fired' : data.disabled.length > 0 ? 'off' : 'ok'}`}>
          {policyBadge(data)}
        </span>}
      </summary>

      {!data && !error && <p className="onb-hint">正在读取自动策略…</p>}
      {error && <p className="onb-error">{error}</p>}

      {data && (
        <>
          <p className="pg-summary">{policySummary(data)}</p>

          <ul className="pg-list">
            {rows.map((row) => (
              <li key={row.id} className={`pg-row pg-${row.status}`}>
                <div className="pg-row-head">
                  <span className={`pg-status pg-status-${row.status}`}>{STATUS_LABEL[row.status]}</span>
                  <span className="pg-label">{row.label}</span>
                </div>
                <div className="pg-detail">{row.detail}</div>
                {row.evidence && <div className="pg-evidence">{row.evidence}</div>}
                <div className="pg-action">动作：{row.action}</div>
              </li>
            ))}
          </ul>

          <p className="onb-hint">{data.note}</p>

          <div className="onb-actions">
            <button type="button" className="secondary" disabled={busy} onClick={load}>
              {busy ? '刷新中…' : '刷新'}
            </button>
          </div>
        </>
      )}
    </details>
  );
}
