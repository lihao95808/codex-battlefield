/**
 * 协调面板：注意账 + 看门狗档位（P4-3 / P4-2）。
 *
 * ## 为什么把两件事放一个面板
 *
 * 它们回答的是同一个问题的两面：**"现在谁在处理什么，以及机器被调成了什么样"**。
 * 分开两个折叠块会让"我调了并发 → 于是看门狗开始处理僵死节点"这条因果链
 * 在界面上断开，而复盘时最先要看的正是这条链。
 *
 * ## 两条显示规矩
 *
 * 1. **认领要显示"谁"与"还剩多久"**。只显示 `stalled_agent` 这种内部词的话，
 *    用户看不出"这是看门狗在处理，不用我管"。
 * 2. **档位的危险方向要标出来**，且**方向**决定危险（安全水位调小才危险）。
 *    只标"这个值很大"会让人误判。
 */
import { useCallback, useEffect, useState } from 'react';

import { applyTuning, fetchAttention, fetchTuning, releaseAttention, rollbackTuning } from './api';
import type { AttentionResponse, TuningResponse } from './types';

function Claims() {
  const [data, setData] = useState<AttentionResponse | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [signature, setSignature] = useState('');

  const load = useCallback(() => {
    fetchAttention()
      .then((payload) => {
        setData(payload);
        setError('');
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onRelease = async (claimId: number, actor: string) => {
    const mine = actor === 'human';
    if (mine && !signature.trim()) {
      setError('强制解除别人的认领要留名 —— 否则事后查不到是谁解的。');
      return;
    }
    setError('');
    try {
      const result = await releaseAttention({
        claimId,
        actor: mine ? 'human' : actor,
        force: mine,
        declaredBy: mine ? signature : undefined,
      });
      setNotice(result.message);
      load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="cw-claims">
      <div className="cw-sub-head">谁在处理（注意账）</div>
      {error && <p className="onb-error">{error}</p>}
      {notice && <p className="onb-notice">{notice}</p>}
      {data && data.count === 0 && (
        <p className="onb-hint">现在没有人认领任何事 —— 管家与看门狗都不会因此重复动手。</p>
      )}
      {data && data.count > 0 && (
        <>
          <ul className="cw-claim-list">
            {data.claims.map((claim) => (
              <li key={claim.id} className="cw-claim">
                <div className="cw-claim-head">
                  <span className="cw-claim-kind">{claim.kind_label}</span>
                  <code className="cw-claim-target">{claim.target || '（全部）'}</code>
                </div>
                <div className="cw-claim-meta">
                  {claim.actor_label} · 还剩 {claim.remaining_seconds}s
                  {claim.reason ? ` · ${claim.reason}` : ''}
                </div>
                <button
                  type="button"
                  className="secondary cw-release"
                  onClick={() => void onRelease(claim.id, claim.actor)}
                >
                  解除
                </button>
              </li>
            ))}
          </ul>
          <label className="onb-field">
            <span className="onb-label">强制解除别人的认领要留名</span>
            <input
              className="onb-input"
              value={signature}
              spellCheck={false}
              onChange={(event) => setSignature(event.target.value)}
            />
          </label>
          <p className="onb-hint">{data.note}</p>
        </>
      )}
      {!data && !error && <p className="onb-hint">正在读取…</p>}
    </div>
  );
}

function Tuning() {
  const [data, setData] = useState<TuningResponse | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [actor, setActor] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(() => {
    fetchTuning()
      .then((payload) => {
        setData(payload);
        setDraft({});
        setError('');
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const changed = Object.keys(draft).filter((key) => {
    const current = data?.current[key];
    return draft[key] !== (typeof current === 'boolean' ? String(current) : String(current ?? ''));
  });

  const submit = async () => {
    if (!changed.length) return;
    if (!actor.trim()) {
      setError('调档必须留名 —— 否则事后无法回答是谁改的。');
      return;
    }
    if (!window.confirm(`调整 ${changed.length} 个档位？\n\n重启 API 后生效。`)) return;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await applyTuning({
        actor,
        changes: changed.map((key) => ({ key, value: draft[key], reason })),
      });
      setNotice(result.message);
      load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const rollback = async () => {
    if (!actor.trim()) {
      setError('回滚必须留名。');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const result = await rollbackTuning({ actor, steps: 1 });
      setNotice(result.message);
      load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cw-tuning">
      <div className="cw-sub-head">看门狗档位</div>
      {error && <p className="onb-error">{error}</p>}
      {notice && <p className="onb-notice">{notice}</p>}
      {data && (
        <>
          <ul className="cw-knob-list">
            {data.catalog.map((item) => {
              const current = data.current[item.key];
              const value = draft[item.key] ?? String(current ?? '');
              return (
                <li key={item.key} className="cw-knob">
                  <div className="cw-knob-head">
                    <span className="cw-knob-label">{item.label}</span>
                    {item.risky_direction && (
                      <span className="cw-knob-risky" title="这个方向变才危险">
                        {item.risky_direction === 'up' ? '调大有风险' : '调小有风险'}
                      </span>
                    )}
                  </div>
                  {item.kind === 'bool' ? (
                    <select
                      className="cfg-select"
                      value={value}
                      disabled={busy}
                      onChange={(event) => setDraft((prev) => ({ ...prev, [item.key]: event.target.value }))}
                    >
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  ) : (
                    <input
                      className="cfg-input"
                      type="number"
                      value={value}
                      min={item.minimum ?? undefined}
                      max={item.maximum ?? undefined}
                      disabled={busy}
                      onChange={(event) => setDraft((prev) => ({ ...prev, [item.key]: event.target.value }))}
                    />
                  )}
                  <div className="cw-knob-help">
                    当前 {String(current)}
                    {item.minimum !== null && item.maximum !== null
                      ? ` · 允许 ${item.minimum}–${item.maximum}`
                      : ''}
                    {' · '}
                    {item.rationale}
                  </div>
                </li>
              );
            })}
          </ul>

          <div className="cw-form">
            <label className="onb-field">
              <span className="onb-label">署名（必填）</span>
              <input
                className="onb-input"
                value={actor}
                spellCheck={false}
                disabled={busy}
                onChange={(event) => setActor(event.target.value)}
              />
            </label>
            <label className="onb-field">
              <span className="onb-label">理由</span>
              <input
                className="onb-input"
                value={reason}
                spellCheck={false}
                disabled={busy}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          </div>

          <div className="onb-actions">
            <button
              type="button"
              className="onb-bind"
              disabled={busy || changed.length === 0}
              onClick={() => void submit()}
            >
              {busy ? '处理中…' : changed.length ? `应用 ${changed.length} 项` : '没有改动'}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy || data.history.length === 0}
              onClick={() => void rollback()}
            >
              回滚最近一次
            </button>
          </div>

          {data.history.length > 0 && (
            <details className="cw-history">
              <summary>调整历史（{data.history.length}）</summary>
              <ul>
                {data.history.map((entry) => (
                  <li key={`${entry.at}-${entry.message}`} className={entry.applied ? '' : 'cw-rolled-back'}>
                    <div className="cw-history-line">{entry.message}</div>
                    <div className="cw-history-meta">
                      {entry.at}
                      {entry.reason ? ` · ${entry.reason}` : ''}
                    </div>
                  </li>
                ))}
              </ul>
            </details>
          )}
          <p className="onb-hint">{data.note}</p>
        </>
      )}
      {!data && !error && <p className="onb-hint">正在读取…</p>}
    </div>
  );
}

export function CoordinationPanel() {
  return (
    <details className="coordination-panel" open>
      <summary>协调：谁在处理 / 档位</summary>
      <p className="onb-hint">
        管家与看门狗都能发现问题。**没有这张账时，同一件事会被处理两次** —— 而不只是
        白干活：它会掩盖真实信号（"这个节点被重试了 3 次"看起来像节点不稳定，
        其实是两个组件各点了一次）。
      </p>
      <Claims />
      <Tuning />
    </details>
  );
}
