/**
 * 状态根与数据**迁移 / 重置**面板（P2-5）。
 *
 * ## 为什么需要它
 *
 * 状态与工程解耦之后，"状态落在哪、占多大、旧库还剩什么"原来只能靠
 * `battlefield-project state show` / `migrate` / `doctor` 去看。
 * 而"换机器、搬状态、清空重来"恰恰是用户**最怕做错**的三件事：
 * 它们不可逆，界面里又没有入口 —— 于是人只能对着命令行犹豫，或者干脆不敢动。
 *
 * ## 两条设计取舍
 *
 * 1. **迁移默认只预演**。预演不写一个字节，先把"迁什么、迁到哪、能不能用"摆出来。
 *    "目标工程选错了"是这里唯一会真正造成损失的错，而它在动手前是可以看清的。
 * 2. **重置要你把 key 抄一遍**。不是仪式：删状态没有后悔药，而按钮比命令行更容易手滑。
 *    后端还会独立核对一次（`confirm` 不匹配就是 409），两道闸管的不是同一件事 ——
 *    界面这道防手滑，后端那道防"有人绕过界面调 API"。
 *
 * ## 一件必须说清的事
 *
 * 删除/迁移**只动状态**，不碰工程目录。用户最怕的其实是"我一点，我的代码没了"，
 * 所以这句话在危险区里必须显式写出来，而不是指望用户自己推断。
 */
import { useCallback, useEffect, useState } from 'react';

import {
  fetchStateOverview,
  migrateState,
  resetProjectState,
} from './api';
import type {
  MigrationApplyResponse,
  MigrationPreviewResponse,
  StateOverviewResponse,
  StateProjectEntry,
} from './types';

/** 字节数转人话 —— 界面上出现 131072 没人读得出来。 */
function humanBytes(value: number): string {
  if (!value) return '0 B';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function ProjectRow({ project }: { project: StateProjectEntry }) {
  const problems: string[] = [];
  if (!project.root_exists) problems.push('工程目录不存在了');
  if (!project.drive_matches) problems.push('与状态目录不同盘 —— git worktree 不可靠');

  return (
    <li className={`sa-project${project.active ? ' sa-project-active' : ''}`}>
      <div className="sa-project-head">
        <code className="sa-project-key">{project.key}</code>
        {project.active && <span className="onb-badge onb-badge-active">当前</span>}
      </div>
      <div className="sa-project-root" title={project.root}>
        {project.root}
      </div>
      <div className="sa-project-meta">
        状态 {humanBytes(project.state_bytes)}（库 {humanBytes(project.db_bytes)}）
      </div>
      {problems.map((text) => (
        <div key={text} className="onb-project-warn">
          {text}
        </div>
      ))}
    </li>
  );
}

function MigrationSection({
  overview,
  onDone,
}: {
  overview: StateOverviewResponse;
  onDone: () => void;
}) {
  const [preview, setPreview] = useState<MigrationPreviewResponse | null>(null);
  const [applied, setApplied] = useState<MigrationApplyResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const run = async (dryRun: boolean) => {
    setBusy(true);
    setError('');
    try {
      const result = await migrateState('', dryRun);
      if (dryRun) {
        setPreview(result as MigrationPreviewResponse);
        setApplied(null);
      } else {
        setApplied(result as MigrationApplyResponse);
        setPreview(null);
        onDone();
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const usable = (preview?.candidates ?? []).filter((item) => item.usable);

  return (
    <details className="sa-section">
      <summary>旧数据迁移（仓库内 workspace → 状态根）</summary>

      {!overview.legacy.present && (
        <p className="onb-hint">
          没有发现旧库 —— 这个工程不需要迁移。迁移只针对"老版本把状态写在仓库里"
          的那种布局。
        </p>
      )}

      {overview.legacy.present && (
        <>
          <p className="onb-hint">
            发现旧库 <code>{overview.legacy.db_path}</code>
            {typeof overview.legacy.db_bytes === 'number' &&
              `（${humanBytes(overview.legacy.db_bytes)}）`}
            。迁移是<b>复制</b>：旧库不会被改动，按内容指纹幂等（迁过的不会产生第二份）。
          </p>

          <div className="onb-actions">
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => void run(true)}
            >
              {busy ? '读取中…' : '预演（不写任何文件）'}
            </button>
          </div>

          {preview && (
            <div className="sa-preview">
              <div className="sa-preview-line">
                任务节点 <b>{preview.legacy_task_count ?? 0}</b> 个 · 工作树记录{' '}
                <b>{preview.legacy_worktree_rows ?? 0}</b> 条
              </div>
              <div className="sa-preview-line">
                落点 key：<code>{preview.target_key ?? '(未绑定工程)'}</code>
              </div>
              {usable.length > 0 && (
                <div className="sa-preview-line">
                  候选工程根：
                  {usable.map((item) => (
                    <code key={item.path} className="sa-candidate">
                      {item.path}
                    </code>
                  ))}
                </div>
              )}
              <div className="onb-actions">
                <button
                  type="button"
                  className="onb-bind"
                  disabled={busy || !preview.target_key}
                  title={
                    preview.target_key
                      ? '把旧库复制到这个工程的状态目录'
                      : '先绑定工程再迁移'
                  }
                  onClick={() => {
                    const label = preview.target_key ?? '';
                    if (
                      !window.confirm(
                        `把旧库复制到工程 ${label} 的状态目录？\n\n` +
                          '旧库不会被删，工程目录也不会被碰。',
                      )
                    ) {
                      return;
                    }
                    void run(false);
                  }}
                >
                  {busy ? '迁移中…' : '执行迁移'}
                </button>
              </div>
            </div>
          )}

          {applied && (
            <p className={applied.copied ? 'onb-notice' : 'onb-hint'}>
              {applied.copied
                ? `已迁移到 ${applied.target_db}（重写工作树记录 ${applied.worktree_rows_rewritten} 条）。${applied.note}`
                : `未迁移：${applied.skipped_reason || '没有需要迁的东西'}`}
            </p>
          )}

          {error && <p className="onb-error">{error}</p>}
        </>
      )}
    </details>
  );
}

function ResetSection({
  overview,
  onDone,
}: {
  overview: StateOverviewResponse;
  onDone: () => void;
}) {
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const active = overview.projects.find((item) => item.active);
  const key = overview.active_key;
  const matches = Boolean(key) && typed.trim() === key;

  const onReset = async () => {
    if (!matches) return;
    if (
      !window.confirm(
        `确认清空工程 ${key} 的**全部状态**？\n\n` +
          `  状态目录：${active?.project_dir ?? '(未知)'}\n` +
          `  占用：${humanBytes(active?.state_bytes ?? 0)}\n\n` +
          '任务、运行记录、工作树、快照都会消失，且**无法恢复**。\n' +
          '工程目录本身不会被碰（你的代码是安全的）。',
      )
    ) {
      return;
    }
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await resetProjectState(key);
      setNotice(result.note);
      setTyped('');
      onDone();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!key) {
    return (
      <details className="sa-section sa-danger">
        <summary>清空状态（不可逆）</summary>
        <p className="onb-hint">当前没有绑定工程，没有可清空的对象。</p>
      </details>
    );
  }

  return (
    <details className="sa-section sa-danger">
      <summary>清空状态（不可逆）</summary>
      <p className="onb-hint">
        删除<b>当前工程</b>的状态目录（
        {humanBytes(active?.state_bytes ?? 0)}）。任务、运行记录、工作树、快照都会消失，
        <b>无法恢复</b>。工程目录本身不会被碰。
      </p>
      <label className="onb-field">
        <span className="onb-label">
          输入工程 key 以确认：<code>{key}</code>
        </span>
        <input
          className="onb-input"
          value={typed}
          spellCheck={false}
          placeholder={key}
          disabled={busy}
          onChange={(event) => setTyped(event.target.value)}
        />
      </label>
      <div className="onb-actions">
        <button
          type="button"
          className="sa-reset"
          disabled={busy || !matches}
          title={matches ? '立即清空状态目录' : '先把上面的 key 完整抄一遍'}
          onClick={() => void onReset()}
        >
          {busy ? '清空中…' : '清空状态'}
        </button>
      </div>
      {error && <p className="onb-error">{error}</p>}
      {notice && <p className="onb-notice">{notice}</p>}
    </details>
  );
}

export function StateAdminPanel() {
  const [data, setData] = useState<StateOverviewResponse | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    fetchStateOverview()
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

  const total = data?.projects.reduce((sum, item) => sum + item.state_bytes, 0) ?? 0;

  return (
    <details className="state-admin">
      <summary>
        状态与数据
        {data && (
          <span className="sa-summary">
            {data.projects.length} 个工程 · 合计 {humanBytes(total)}
            {data.legacy.present && ' · 有旧库待迁移'}
          </span>
        )}
      </summary>

      {!data && !error && <p className="onb-hint">正在读取状态概览…</p>}
      {error && <p className="onb-error">{error}</p>}

      {data && (
        <>
          <div className="sa-line">
            <span className="sa-line-label">状态根</span>
            <code title={data.state_home}>{data.state_home}</code>
          </div>
          <div className="sa-line">
            <span className="sa-line-label">配置文件</span>
            <code title={data.config_file}>{data.config_file}</code>
          </div>

          {/* "切了工程但没重启"时必须显眼地说 —— 否则用户以为已经切过去了。 */}
          {!data.snapshot_matches_active_file && (
            <p className="sa-stale">
              当前进程连的还是启动时那个工程 —— 磁盘上已经切成{' '}
              <code>{data.active_key || '(未绑定)'}</code>，但<b>要重启 API 才生效</b>。
            </p>
          )}

          <ul className="sa-list">
            {data.projects.map((item) => (
              <ProjectRow key={item.key} project={item} />
            ))}
          </ul>
          {data.projects.length === 0 && (
            <p className="onb-hint">还没有任何已挂载工程。</p>
          )}

          <MigrationSection overview={data} onDone={load} />
          <ResetSection overview={data} onDone={load} />

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
