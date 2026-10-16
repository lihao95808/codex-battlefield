/**
 * 运行时配置面板（P2-2）。
 *
 * ## 为什么需要它
 *
 * 要改一个设置（比如"让管家能执行动作"或"推送别这么烦"），原来得：
 * 找到配置文件 → 查键名 → 手写 TOML → 重启 API。而键名不是显然的
 * （`steward_proactive_min_interval_seconds` 谁会记得），写错了还不报错。
 *
 * ## 三件必须说清楚的事
 *
 * 1. **改了要重启才生效**。`Settings` 是启动时构造一次的 frozen dataclass，
 *    所以保存只写文件、不改变当前进程。不说这句话，用户会以为保存即生效。
 * 2. **被环境变量压着的项改了没用**。优先级是「环境变量 > 文件 > 默认」，
 *    所以界面对这类项要显式标注 —— 否则用户会反复保存、反复重启、反复疑惑。
 * 3. **危险项要二次确认**。放宽沙箱、打开主动汇报都是"点一下下拉框就生效"，
 *    事后很难归因。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { fetchSettings, saveSettings } from './api';
import type { SettingField, SettingsResponse } from './types';

/** 把任意值转成输入框用的字符串（后端负责类型转换，这里不做判断）。 */
function toInput(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
}

function FieldEditor({
  field,
  value,
  disabled,
  onChange,
}: {
  field: SettingField;
  value: string;
  disabled: boolean;
  onChange: (next: string) => void;
}) {
  if (field.kind === 'bool') {
    return (
      <input
        type="checkbox"
        className="cfg-check"
        checked={value === 'true'}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked ? 'true' : 'false')}
      />
    );
  }
  if (field.choices.length > 0) {
    return (
      <select
        className="cfg-select"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {field.choices.map((choice) => (
          <option key={choice || '(空)'} value={choice}>
            {choice === '' ? '(留空 = 用默认)' : choice}
          </option>
        ))}
      </select>
    );
  }
  return (
    <input
      className="cfg-input"
      type={field.kind === 'str' ? 'text' : 'number'}
      value={value}
      spellCheck={false}
      disabled={disabled}
      min={field.minimum ?? undefined}
      max={field.maximum ?? undefined}
      step={field.kind === 'float' ? 'any' : undefined}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function SettingsPanel() {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    fetchSettings()
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

  const fields = useMemo(() => data?.fields ?? [], [data]);
  const grouped = useMemo(() => {
    const order = data?.order ?? [];
    return order
      .map((group) => ({
        group,
        label: data?.groups[group] ?? group,
        items: fields.filter((field) => field.group === group),
      }))
      .filter((entry) => entry.items.length > 0);
  }, [data, fields]);

  /** 草稿里"与当前生效值不同"的项 —— 只有这些会被提交。 */
  const changed = useMemo(() => {
    if (!data) return [] as string[];
    return Object.keys(draft).filter((key) => {
      const current = toInput(data.current[key]?.value);
      return draft[key] !== current;
    });
  }, [data, draft]);

  const riskyChanged = useMemo(
    () => changed.filter((key) => fields.find((field) => field.key === key)?.risky),
    [changed, fields],
  );

  const onSave = async () => {
    if (!changed.length) return;
    if (riskyChanged.length) {
      const labels = riskyChanged
        .map((key) => fields.find((field) => field.key === key)?.label ?? key)
        .join('、');
      // 二次确认：这些项会放宽权限或增加开销，手滑一下就生效太危险。
      const ok = window.confirm(
        `以下改动会放宽权限或增加开销：\n\n${labels}\n\n确认要保存吗？`,
      );
      if (!ok) return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const values: Record<string, string> = {};
      for (const key of changed) values[key] = draft[key];
      const result = await saveSettings(values, riskyChanged.length > 0);
      setNotice(result.message);
      load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="settings-panel" aria-label="设置">
      <div className="cfg-head">
        <h3>设置</h3>
        {data && <span className="cfg-restart">改完要重启 API</span>}
      </div>

      {!data && !error && <p className="cfg-hint">正在读取设置…</p>}
      {error && <p className="cfg-error">{error}</p>}
      {notice && <p className="cfg-notice">{notice}</p>}

      {data && (
        <>
          <p className="cfg-hint">
            只列出<b>会被读取</b>的项 —— 只认环境变量的档位不在这里（写了也不生效）。
            配置文件：<code>{data.config_file}</code>
          </p>
          {grouped.map((entry) => (
            <details key={entry.group} className="cfg-group">
              <summary>{entry.label}</summary>
              <div className="cfg-fields">
                {entry.items.map((field) => {
                  const current = data.current[field.key];
                  const value = draft[field.key] ?? toInput(current?.value);
                  const dirty = value !== toInput(current?.value);
                  return (
                    <div key={field.key} className={`cfg-field${dirty ? ' cfg-field-dirty' : ''}`}>
                      <div className="cfg-field-head">
                        <span className="cfg-label">
                          {field.label}
                          {field.risky && <span className="cfg-risky">危险</span>}
                        </span>
                        {/* 被环境变量压着 = 在这里改没用。这是最容易让人白忙一场的一种状态。 */}
                        {current?.env_override && (
                          <span className="cfg-env" title={`环境变量 ${field.env} 正在压过配置文件`}>
                            被环境变量覆盖
                          </span>
                        )}
                      </div>
                      <FieldEditor
                        field={field}
                        value={value}
                        disabled={busy}
                        onChange={(next) => setDraft((prev) => ({ ...prev, [field.key]: next }))}
                      />
                      {field.help && <p className="cfg-help">{field.help}</p>}
                    </div>
                  );
                })}
              </div>
            </details>
          ))}

          <div className="cfg-actions">
            <button
              type="button"
              className="cfg-save"
              disabled={busy || changed.length === 0}
              onClick={() => void onSave()}
            >
              {busy ? '保存中…' : changed.length ? `保存 ${changed.length} 项` : '没有改动'}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={load}>
              重新读取
            </button>
          </div>
        </>
      )}
    </section>
  );
}
