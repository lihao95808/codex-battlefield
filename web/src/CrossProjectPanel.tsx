/**
 * 跨工程总览面板（P3-4）。
 *
 * ## 为什么需要它
 *
 * 状态与工程解耦之后，每个工程有自己的库，而进程**同一时刻只连一个**（决策 D1：
 * 不热切库）。于是"我所有工程现在怎么样"在界面上原本无解 —— 看板只看得见当前
 * 工程，管家也只能答当前工程。用户要逐个切换、重启，才知道别处有没有卡住。
 *
 * ## 三条显示规矩（都由 `crossProject.ts` 的纯函数 + 测试钉住）
 *
 * 1. **按"要不要我动手"排序**：读不到 > 等你处理 > 在跑 > 空闲。
 *    用户从上往下看就行，不需要自己判断哪一行更急。
 * 2. **"读不到"绝不显示成 `0`**。后端给 `null` 就是"读不到"，给 `0` 才是"真的是零"。
 *    这一条最容易在手写 JSX 时被 `?? 0` 抹掉，而后果是"还没跑过"变成"工程是空的"。
 * 3. **金额按币种分别显示**，不做跨币种合计 —— 那个数字在任何币种下都是错的。
 *
 * 另外：合计**偏小**时（有工程读不到）必须显式说出来。少算的钱比不报更危险。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchCrossProject } from './api';
import {
  VERDICT_LABEL,
  countText,
  costLines,
  humanBytes,
  moneyText,
  sortByVerdict,
  tokenText,
  totalsText,
} from './crossProject';
import type { CrossProjectEntry, CrossProjectResponse } from './types';

function ProjectRow({ entry }: { entry: CrossProjectEntry }) {
  const problems: string[] = [];
  if (!entry.root_exists) problems.push('工程目录不存在了');
  if (entry.available && (entry.open_blocking_obligations ?? 0) > 0) {
    problems.push(`${entry.open_blocking_obligations} 条未清阻断债`);
  }

  return (
    <li className={`xp-project xp-verdict-${entry.verdict}`}>
      <div className="xp-project-head">
        <span className={`xp-badge xp-badge-${entry.verdict}`}>{VERDICT_LABEL[entry.verdict]}</span>
        <code className="xp-project-key">{entry.key}</code>
        {entry.active && <span className="onb-badge onb-badge-active">当前</span>}
      </div>

      {entry.lead_goal && (
        <div className="xp-goal" title={entry.lead_goal}>
          {entry.lead_goal}
        </div>
      )}

      <div className="xp-project-meta">
        任务 {countText(entry.task_total)} · 状态{' '}
        {entry.available
          ? Object.entries(entry.task_by_status)
              .map(([status, count]) => `${status} ${count}`)
              .join(' / ') || '（无节点）'
          : entry.reason}
      </div>

      {entry.available && (
        <div className="xp-project-meta">
          状态占用 {humanBytes(entry.state_bytes)}
          {entry.latest_event ? ` · 最近事件：${entry.latest_event.message}` : ''}
        </div>
      )}

      {problems.map((text) => (
        <div key={text} className="onb-project-warn">
          {text}
        </div>
      ))}
    </li>
  );
}

export function CrossProjectPanel() {
  const [data, setData] = useState<CrossProjectResponse | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    fetchCrossProject()
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

  const ordered = useMemo(() => sortByVerdict(data?.projects ?? []), [data]);
  const costs = useMemo(() => (data ? costLines(data.totals) : []), [data]);

  return (
    <details className="cross-project" open={(data?.projects.length ?? 0) > 1}>
      <summary>
        所有工程
        {data && <span className="xp-count">{data.totals.project_count}</span>}
      </summary>

      {!data && !error && <p className="onb-hint">正在读取各工程…</p>}
      {error && <p className="onb-error">{error}</p>}

      {data && (
        <>
          <p className="xp-totals">{totalsText(data.totals)}</p>

          {costs.length > 0 && (
            <p className="xp-costs">
              花费：{costs.map((line) => moneyText(line)).join(' · ')}
            </p>
          )}
          {costs.length === 0 && (
            // 不给 0：0 会被读成"跑了但没花钱"，而真相是"没有可用的计价数据"。
            <p className="xp-costs xp-costs-unknown">花费：没有可用的计价数据</p>
          )}
          <p className="onb-hint">
            合计 token {tokenText(data.totals.total_tokens)} · 状态根 {data.state_home}
          </p>

          {data.notes.length > 0 && (
            <ul className="xp-notes">
              {data.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          )}

          <ul className="xp-list">
            {ordered.map((entry) => (
              <ProjectRow key={entry.key} entry={entry} />
            ))}
          </ul>
          {ordered.length === 0 && <p className="onb-hint">还没有任何已挂载工程。</p>}

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
