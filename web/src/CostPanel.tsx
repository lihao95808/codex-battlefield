/**
 * 花费面板（P3-3 / P3-2）。
 *
 * ## 为什么需要它
 *
 * 跑一棵树最贵的代价不是 token，而是**不知道要花多少**。原来只有一个事后合计，
 * 于是用户只能"跑起来看看"，而"看看"的代价是真金白银。
 *
 * 这个面板回答两个问题：
 *
 * * **钱花在哪一层**（按节点 / 角色 / 模型拆开）；
 * * **跑完还要多少**（按已完成节点的实际花费给一个区间）。
 *
 * ## 三条显示规矩（由 `costView.ts` 的纯函数 + 测试钉住）
 *
 * 1. **预测必须给区间 + 样本量**，绝不只给一个数。孤零零的"预计还需 12.34 元"
 *    会被当成承诺，而用户可能据此决定去睡觉。
 * 2. **算不出来就说算不出来**（显示原因），不显示 0 —— 0 会被读成"不用再花钱了"。
 * 3. **缺数据的提醒必须显眼**（没有用量的运行、没有模型名的运行）。
 *    不报的话，归因表看起来完整，而它其实漏掉了一块。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchCost } from './api';
import {
  WEAK_SAMPLE_NODES,
  amountText,
  attributionWarnings,
  bucketRow,
  projectionText,
  sortRowsByCost,
  type NamedCostRow,
} from './costView';
import type { CostReport } from './types';

function Row({ row, label }: { row: NamedCostRow; label?: string }) {
  return (
    <li className="cg-row">
      <div className="cg-row-head">
        <span className="cg-row-label" title={row.label}>
          {label ?? row.label}
        </span>
        <span className="cg-row-amount">
          {row.costs.length > 0
            ? row.costs.map((item) => amountText(item.code, item.amount)).join(' · ')
            : '无金额'}
        </span>
      </div>
      <div className="cg-row-meta">
        {row.runs} 次运行 · {row.totalTokens} token
      </div>
    </li>
  );
}

function Breakdown({ title, rows }: { title: string; rows: NamedCostRow[] }) {
  if (rows.length === 0) {
    return (
      <details className="cg-section">
        <summary>{title}</summary>
        <p className="onb-hint">这一维还没有数据。</p>
      </details>
    );
  }
  return (
    <details className="cg-section" open>
      <summary>
        {title}
        <span className="cg-count">{rows.length}</span>
      </summary>
      <ul className="cg-list">
        {rows.map((row) => (
          <Row key={row.key} row={row} />
        ))}
      </ul>
    </details>
  );
}

export function CostPanel() {
  const [data, setData] = useState<CostReport | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    fetchCost()
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

  const projection = data ? projectionText(data.projection) : null;
  const warnings = useMemo(() => (data ? attributionWarnings(data) : []), [data]);
  const byNode = useMemo(
    () =>
      sortRowsByCost(
        (data?.attribution.by_node ?? []).map((bucket) =>
          bucketRow(bucket, bucket.title ? `${bucket.title}` : bucket.key),
        ),
      ),
    [data],
  );
  const byKind = useMemo(
    () => sortRowsByCost((data?.attribution.by_kind ?? []).map((bucket) => bucketRow(bucket))),
    [data],
  );
  const byModel = useMemo(
    () => sortRowsByCost((data?.attribution.by_model ?? []).map((bucket) => bucketRow(bucket))),
    [data],
  );

  return (
    <details className="cost-panel">
      <summary>
        花费
        {data && (
          <span className="cg-count">
            {data.attribution.total_tokens} token / {data.node_counts.total} 节点
          </span>
        )}
      </summary>

      {!data && !error && <p className="onb-hint">正在读取花费…</p>}
      {error && <p className="onb-error">{error}</p>}

      {data && (
        <>
          <div className="cg-projection">
            <div className="cg-projection-head">跑完还要多少</div>
            {projection?.available ? (
              <>
                <div className="cg-range">{projection.range}</div>
                <div className="cg-basis">{projection.basis}</div>
                {/* 样本太少时明说 —— 这条是"别拿它当承诺"的唯一提示。 */}
                {projection.weakSample && (
                  <div className="cg-weak">
                    样本不足 {WEAK_SAMPLE_NODES} 个节点，这个区间**很粗**：它只说明量级，
                    不能当作承诺。
                  </div>
                )}
              </>
            ) : (
              <div className="cg-unavailable">
                {/* 显示原因而不是 0：0 会被读成"不用再花钱了"。 */}
                {projection?.reason}
              </div>
            )}
          </div>

          <p className="cg-total">
            累计{' '}
            {data.attribution.cost_by_currency
              ? Object.entries(data.attribution.cost_by_currency)
                  .map(([code, amount]) => amountText(code, amount))
                  .join(' · ')
              : '没有可用的计价数据'}
            {' · '}
            已结算 {data.node_counts.settled} / 剩余 {data.node_counts.remaining}
          </p>

          {warnings.map((text) => (
            <p key={text} className="cg-warning">
              {text}
            </p>
          ))}

          <Breakdown title="按节点" rows={byNode} />
          <Breakdown title="按角色" rows={byKind} />
          <Breakdown title="按模型" rows={byModel} />

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
