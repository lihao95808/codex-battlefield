/**
 * 目标投递与进度面板（P4-1）。
 *
 * ## 为什么需要它
 *
 * 管家起完树就与那棵树失去联系，用户只能自己去翻看板问"第几步了？卡在哪？"。
 * 这个面板把这两句话变成可核对的数：`已完成 12 / 总计 30` + **最深的未完成节点**
 * + **卡住的地方**。
 *
 * ## 两条显示规矩
 *
 * 1. **不报 0/0**。读不到时说"读不到 + 原因" —— 报 0/0 看起来像"还没开始"，
 *    而真相可能是读错了树（换工程、删库）。
 * 2. **卡点优先于进度**。有阻断债或卡住节点时先说那个；否则用户看到"进度 90%"
 *    会以为一切正常，直到很久之后才发现有人在等。
 */
import { useCallback, useEffect, useState } from 'react';

import { fetchDelivery } from './api';
import { progressText, ratioText, shouldExpand } from './deliveryView';
import type { DeliveryResponse } from './types';

export function DeliveryPanel() {
  const [data, setData] = useState<DeliveryResponse | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    fetchDelivery()
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

  const progress = data?.progress ?? null;

  return (
    <details className="delivery-panel" open={shouldExpand(progress)}>
      <summary>
        目标投递
        {progress?.available && (
          <span className="dp-count">
            {progress.done}/{progress.total}
          </span>
        )}
      </summary>

      {!data && !error && <p className="onb-hint">正在读取投递进度…</p>}
      {error && <p className="onb-error">{error}</p>}

      {data && !data.available && (
        <p className="onb-hint">
          {data.reason}
          <br />
          通过管家投递一个目标（或让管家提议 <code>start_task</code>）之后，这里会显示
          "第几步、卡在哪"。
        </p>
      )}

      {data?.delivery && (
        <>
          <div className="dp-goal" title={data.delivery.goal}>
            {data.delivery.goal || '（没有目标描述）'}
          </div>
          <div className="dp-meta">
            根节点 {data.delivery.root_id}
            {data.delivery.author ? ` · ${data.delivery.author}` : ''}
            {data.delivery.created_at ? ` · ${data.delivery.created_at}` : ''}
          </div>
        </>
      )}

      {progress && !progress.available && (
        // 读不到时说清原因，**不报 0/0**。
        <p className="onb-error">{progress.reason}</p>
      )}

      {progress?.available && (
        <>
          <div className="dp-progress">
            <span className="dp-ratio">{ratioText(progress.ratio)}</span>
            <span className="dp-progress-text">{progressText(progress)}</span>
          </div>

          <p className="dp-next">{progress.next_step}</p>

          {progress.blocking_obligations.length > 0 && (
            <div className="dp-blockers">
              <div className="dp-section-title">未清阻断债（{progress.blocking_obligations.length}）</div>
              <ul>
                {progress.blocking_obligations.map((item) => (
                  <li key={item.id || `${item.type}-${item.node_id}`}>
                    <code>{item.node_id}</code> {item.type}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {progress.stuck.length > 0 && (
            <div className="dp-blockers">
              <div className="dp-section-title">卡住的节点（{progress.stuck.length}）</div>
              <ul>
                {progress.stuck.map((node) => (
                  <li key={node.id}>
                    <code>{node.id}</code> {node.status}
                    {node.error ? ` · ${node.error}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {progress.frontier.length > 0 && (
            <div className="dp-frontier">
              <div className="dp-section-title">前线（最深的未完成节点）</div>
              <ul>
                {progress.frontier.map((node) => (
                  <li key={node.id}>
                    <code>{node.id}</code> 第 {node.depth} 层 · {node.status}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {progress.incomplete.length > 0 && (
            <ul className="dp-incomplete">
              {progress.incomplete.map((text) => (
                <li key={text}>{text}</li>
              ))}
            </ul>
          )}
        </>
      )}

      {data && (
        <div className="onb-actions">
          <button type="button" className="secondary" disabled={busy} onClick={load}>
            {busy ? '刷新中…' : '刷新'}
          </button>
        </div>
      )}
    </details>
  );
}
