/**
 * 预授权面板（P4-4）。
 *
 * ## 这个面板要防的是什么
 *
 * 预授权是**权限模型的实质性放宽**。而"放宽"在界面上最容易做成一个勾选框：
 * 勾一下，从此不需要人点。所以这里刻意不做成勾选框，而是要求三样东西：
 *
 * 1. **勾选具体动作**（不是"全部"）—— 危险动作在列表里且**不可勾**；
 * 2. **填期限（分钟）** —— 没有"永久"这个选项；
 * 3. **签名** —— 事后要能回答"这条授权是谁给的"。
 *
 * ## 界面上最要紧的一句
 *
 * 高风险动作**在列表里显示为不可预授权**，并写明"永远需要你确认"。
 * 只把它们藏起来的话，用户会以为"列表里没有 = 也可以预授权"，或者反过来
 * 反复找不到 `cancel_all` 而以为工具坏了。
 *
 * 勾选状态由纯函数 `preAuthDraft.ts` 管（可测），组件只负责摆版式。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchPreAuthorization, grantPreAuthorization, revokePreAuthorization } from './api';
import {
  RISK_ORDER,
  grantWarning,
  remainingText,
  riskLabel,
  selectableActions,
  toggleAction,
  validateDraft,
} from './preAuthDraft';
import type { PreAuthorizationResponse } from './types';

export function PreAuthPanel() {
  const [data, setData] = useState<PreAuthorizationResponse | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [minutes, setMinutes] = useState('30');
  const [signature, setSignature] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(() => {
    fetchPreAuthorization()
      .then((payload) => {
        setData(payload);
        setError('');
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const catalog = useMemo(
    () => [...(data?.catalog ?? [])].sort((a, b) => RISK_ORDER[a.risk] - RISK_ORDER[b.risk]),
    [data],
  );
  const selectable = useMemo(() => selectableActions(data?.catalog ?? []), [data]);

  const onGrant = async () => {
    const problem = validateDraft({ actions: selected, minutes, signature });
    if (problem.field) {
      setError(problem.message);
      return;
    }
    // 局部变量名不能叫 `window`：那会遮住全局的 `window.confirm`。
    const minutesValue = Number(minutes);
    if (
      !window.confirm(
        `授予预授权？\n\n  动作：${selected.join('、')}\n  期限：${minutesValue} 分钟\n  署名：${signature}\n\n` +
          '高风险动作（取消、全停、改额度闸）**不在此范围内**，仍然需要你逐个确认。',
      )
    ) {
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await grantPreAuthorization({
        actions: selected,
        targets: [],
        minutes: minutesValue,
        grantedBy: signature,
        reason,
      });
      setNotice(result.message);
      setSelected([]);
      load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const onRevoke = async () => {
    setBusy(true);
    setError('');
    try {
      const result = await revokePreAuthorization();
      setNotice(result.message);
      load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const grant = data?.grant ?? null;

  return (
    <details className="preauth-panel" open={Boolean(grant)}>
      <summary>
        预授权
        <span className={`pa-badge ${grant ? 'pa-badge-on' : 'pa-badge-off'}`}>
          {grant ? `生效中（${grant.remaining_seconds}s）` : '未授权'}
        </span>
      </summary>

      {!data && !error && <p className="onb-hint">正在读取预授权状态…</p>}
      {error && <p className="onb-error">{error}</p>}

      {data && (
        <>
          <p className="onb-hint">
            预授权放宽的是<b>确认次数</b>，不是权限边界。默认没有授权 ——
            出厂状态是每个动作都要你点。高风险动作在任何授权下都仍需确认。
          </p>

          {grant && (
            <div className="pa-active">
              <div className="pa-active-line">动作：{grant.actions.join('、')}</div>
              <div className="pa-active-line">
                目标：{grant.targets.length ? grant.targets.join('、') : '不限'}
              </div>
              <div className="pa-active-line">
                到期：{grant.expires_at}（{remainingText(grant)}）
              </div>
              <div className="pa-active-line">
                署名：{grant.granted_by}
                {grant.reason ? ` · 理由：${grant.reason}` : ''}
              </div>
              <div className="pa-warning">{grantWarning(grant)}</div>
            </div>
          )}

          <ul className="pa-list">
            {catalog.map((item) => {
              const disabled = !item.pre_authorizable;
              const checked = selected.includes(item.action);
              return (
                <li key={item.action} className={`pa-row pa-${item.risk}`}>
                  <label className="pa-row-head">
                    <input
                      type="checkbox"
                      className="pa-check"
                      checked={checked && !disabled}
                      disabled={disabled || busy}
                      onChange={() =>
                        setSelected((prev) => toggleAction(prev, item.action, item.pre_authorizable))
                      }
                    />
                    <code className="pa-action">{item.action}</code>
                    <span className={`pa-risk pa-risk-${item.risk}`}>{riskLabel(item)}</span>
                  </label>
                  <div className="pa-rationale">{item.rationale}</div>
                </li>
              );
            })}
          </ul>

          <div className="pa-form">
            <label className="onb-field">
              <span className="onb-label">期限（分钟，必填）</span>
              <input
                className="onb-input"
                type="number"
                min={1}
                value={minutes}
                disabled={busy}
                onChange={(event) => setMinutes(event.target.value)}
              />
            </label>
            <label className="onb-field">
              <span className="onb-label">署名（必填）</span>
              <input
                className="onb-input"
                value={signature}
                spellCheck={false}
                disabled={busy}
                placeholder="谁批的"
                onChange={(event) => setSignature(event.target.value)}
              />
            </label>
            <label className="onb-field">
              <span className="onb-label">理由（可选）</span>
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
              disabled={busy || selected.length === 0}
              onClick={() => void onGrant()}
            >
              {busy ? '处理中…' : `授予预授权（${selected.length} 个动作）`}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy || !grant}
              onClick={() => void onRevoke()}
            >
              撤销
            </button>
          </div>

          <p className="onb-hint">
            可勾选的动作共 {selectable.length} 个；其余是高风险，永远需要逐个确认。
          </p>
          {notice && <p className="onb-notice">{notice}</p>}
          <p className="onb-hint">{data.note}</p>
        </>
      )}
    </details>
  );
}
