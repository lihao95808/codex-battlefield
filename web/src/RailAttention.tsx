import { useEffect, useState } from 'react';
import { subscribeStewardAlerts } from './api';
import type { StateLayoutResponse, StewardAlert } from './types';

/** 提醒不跟着辅助工具折叠：侧栏变短不等于把待审批和绑定失配藏起来。 */
export function RailAttention({ layout }: { layout: StateLayoutResponse | null }) {
  const [alerts, setAlerts] = useState<StewardAlert[]>([]);
  const [error, setError] = useState('');
  useEffect(() => subscribeStewardAlerts((push) => {
    setAlerts(push.alerts.filter((alert) => alert.level === 'warning' || alert.level === 'action' || alert.kind === 'pending_actions'));
    setError('');
  }, setError), []);
  return (
    <div className="rail-attention" aria-live="polite">
      {layout?.bound && <p className="rail-mode-note" title={layout.project_root ?? undefined}>
        当前工程：{layout.project_root?.split(/[\\/]/).filter(Boolean).pop() ?? '未知'}
      </p>}
      {layout && !layout.snapshot_matches_active_file && <p className="error" role="alert">
        工程绑定已变更，但当前进程仍使用启动时的工程。请重启 API 后继续。
      </p>}
      {alerts.map((alert) => <p key={`${alert.kind}:${alert.message}`} className="rail-warning">{alert.message}</p>)}
      {error && <p className="error" role="alert">提醒读取失败：{error}</p>}
    </div>
  );
}
