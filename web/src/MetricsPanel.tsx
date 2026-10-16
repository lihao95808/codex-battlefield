/**
 * 端点耗时面板（P5-1 / P5-5）。
 *
 * ## 为什么要放在界面上
 *
 * 指标只有被看见才有用。放在 `/api/metrics` 里等人 curl，等于没人看 —— 而"这个端点
 * 比上周慢了 5 倍"是那种**不看就永远不知道**的问题。
 *
 * ## 两条显示规矩
 *
 * 1. **退化要显眼**：超过基线 1.5 倍的路由排在最前面并标红。
 *    埋在长列表里的退化等于没报。
 * 2. **未量到的显示 `—`，不显示 `0ms`**。0ms 看起来像"快得看不见"，
 *    而真相是"还没量到" —— 这两件事的下一步动作完全不同。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchMetrics } from './api';
import { badgeText, errorRateText, msText, orderedRoutes } from './metricsView';
import type { MetricsResponse } from './types';

export function MetricsPanel() {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    fetchMetrics(12)
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

  const regressed = useMemo(
    () => new Set((data?.regressions ?? []).map((item) => item.key)),
    [data],
  );
  const rows = useMemo(() => (data ? orderedRoutes(data) : []), [data]);

  return (
    <details className="metrics-panel" open={(data?.regressions.length ?? 0) > 0}>
      <summary>
        端点耗时
        {data && (
          <span className={`mp-badge ${data.regressions.length > 0 ? 'mp-badge-warn' : 'mp-badge-ok'}`}>
            {badgeText(data)}
          </span>
        )}
      </summary>

      {!data && !error && <p className="onb-hint">正在读取指标…</p>}
      {error && <p className="onb-error">{error}</p>}

      {data && (
        <>
          {data.regressions.length > 0 && (
            <ul className="mp-regressions">
              {data.regressions.map((item) => (
                <li key={item.key}>
                  退化 <code>{item.key}</code>：{msText(item.baseline_p95_ms)} →{' '}
                  {msText(item.current_p95_ms)}（{item.ratio}×）
                </li>
              ))}
            </ul>
          )}

          <table className="mp-table">
            <thead>
              <tr>
                <th>端点</th>
                <th>p50</th>
                <th>p95</th>
                <th>错误率</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((route) => (
                <tr key={route.key} className={regressed.has(route.key) ? 'mp-row-warn' : ''}>
                  <td className="mp-key" title={route.key}>
                    {route.key}
                  </td>
                  <td>{msText(route.p50_ms)}</td>
                  <td>{msText(route.p95_ms)}</td>
                  <td>{errorRateText(route.error_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="onb-hint">{data.note}</p>
          <p className="onb-hint">
            运行 {data.uptime_seconds}s · 基线 {Object.keys(data.baseline).length} 条
          </p>

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
