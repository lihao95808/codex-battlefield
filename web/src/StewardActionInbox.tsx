/**
 * 管家的动作收件箱（P5-2：从 `StewardPanel.tsx` 拆出来）。
 *
 * `actionStatus` / `shortTime` 导出给面板复用 —— 不复制第二份实现。
 * 这个文件里只有 `ActionInbox` 一个组件和它的两个纯函数，所以不需要 React hooks，
 * 也不需要直接调 API（那些在面板里）。
 */

import type { StewardActionRecord } from './types';

export const ACTION_STATUS: Record<string, { label: string; tone: string }> = {
  pending: { label: '等你决定', tone: 'wait' },
  approved: { label: '已批准', tone: 'ok' },
  executed: { label: '已执行', tone: 'ok' },
  rejected: { label: '已拒绝', tone: 'muted' },
  failed: { label: '执行失败', tone: 'bad' },
};

export function actionStatus(status: string): { label: string; tone: string } {
  return ACTION_STATUS[status] ?? { label: status, tone: 'muted' };
}

export function shortTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString();
}

export function ActionInbox({
  actions,
  readOnly,
  busyId,
  onDecide,
}: {
  actions: StewardActionRecord[];
  readOnly: boolean;
  busyId: number | null;
  onDecide: (action: StewardActionRecord, approve: boolean) => void;
}) {
  if (!actions.length) return null;

  const pending = actions.filter((row) => row.status === 'pending');
  const settled = actions.filter((row) => row.status !== 'pending');

  const renderRow = (row: StewardActionRecord) => {
    const status = actionStatus(row.status);
    const waiting = row.status === 'pending';
    const busy = busyId === row.id;
    return (
      <li key={row.id} className={`steward-action steward-action-${status.tone}`}>
        <div className="steward-action-head">
          <code className="steward-action-name">{row.action}</code>
          {row.target && <span className="steward-action-target">{row.target}</span>}
          <span className={`steward-action-status steward-tone-${status.tone}`}>{status.label}</span>
        </div>
        {row.why && <p className="steward-action-why">{row.why}</p>}
        {row.risk && <p className="steward-action-risk">风险：{row.risk}</p>}
        {row.result && <p className="steward-action-result">{row.result}</p>}
        {row.error && <p className="steward-action-error">没能做成：{row.error}</p>}
        {row.executed_at && (
          <p className="steward-action-time">执行于 {shortTime(row.executed_at)}</p>
        )}

        {waiting && (
          <div className="steward-action-actions">
            <button
              type="button"
              className="steward-approve"
              disabled={readOnly || busy}
              onClick={() => onDecide(row, true)}
              title={readOnly ? '管家只读模式下不能代为执行' : '批准并立即执行'}
            >
              {busy ? '执行中…' : '批准并执行'}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => onDecide(row, false)}
            >
              拒绝
            </button>
          </div>
        )}
      </li>
    );
  };

  return (
    <div className="steward-inbox" aria-label="管家动作收件箱">
      <div className="steward-inbox-head">
        <h4>待你决定的动作</h4>
        {pending.length > 0 && <span className="steward-inbox-count">{pending.length}</span>}
      </div>
      {readOnly && pending.length > 0 && (
        <p className="steward-inbox-note">
          只读模式下可以提议、不能执行。要让它能执行，设
          <code>CODEX_BATTLEFIELD_STEWARD_READ_ONLY=0</code> 后重启 API。
        </p>
      )}
      {pending.length > 0 && <ul className="steward-action-list">{pending.map(renderRow)}</ul>}
      {settled.length > 0 && (
        <details className="steward-actions-settled">
          <summary>已决定的（{settled.length}）</summary>
          <ul className="steward-action-list">{settled.map(renderRow)}</ul>
        </details>
      )}
    </div>
  );
}
