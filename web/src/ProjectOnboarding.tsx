/**
 * 工程绑定面板（P1-5：首次运行引导）。
 *
 * ## 为什么需要它
 *
 * 未绑定工程时，界面原来只在侧栏里写一行小字「未绑定工程（每工程状态回退仓库内 workspace）」
 * —— 它**描述了状态，但没说下一步做什么**。新用户到这里就卡住了：不知道要绑、
 * 不知道绑什么、也不知道得回到命令行去敲 `battlefield-project use`。
 *
 * ## 三条必须说清楚的事（都是实测踩过的）
 *
 * 1. **绑好了 ≠ 生效了**。绑定写的是 `active.json`，而当前 API 进程的库连接与
 *    工作目录在启动时就定下了（决策 D1：不热切库）。所以必须显式说"要重启"，
 *    否则用户会以为点了就切过去了，然后对着旧工程的数据发懵。
 * 2. **跨盘不能用**。状态目录与工程不同盘时 `git worktree` 有已知兼容问题。
 *    这是"能绑但会出错"，得在绑的那一刻就提醒。
 * 3. **工程目录可能已经没了**。绑过之后把目录删掉/移走是很常见的，
 *    这时要显眼地说"这个工程的目录不存在了"，而不是照常显示、等跑任务才失败。
 */
import { useCallback, useEffect, useState } from 'react';

import { bindProject, fetchProjects } from './api';
import type { MountedProject, MountedProjectsResponse } from './types';

/** 字节数转人话（只为了不让界面上出现 131072 这种读不出来的数字）。 */
function humanBytes(value: number): string {
  if (!value) return '空库';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function ProjectRow({
  project,
  busy,
  onBind,
}: {
  project: MountedProject;
  busy: boolean;
  onBind: (path: string) => void;
}) {
  const problems: string[] = [];
  if (!project.root_exists) problems.push('工程目录不存在了');
  if (!project.drive_matches) problems.push('状态目录与工程不同盘 —— git worktree 不可靠');

  return (
    <li className={`onb-project${project.active ? ' onb-project-active' : ''}`}>
      <div className="onb-project-head">
        <code className="onb-project-key">{project.key}</code>
        {project.active && <span className="onb-badge onb-badge-active">当前</span>}
      </div>
      <div className="onb-project-root" title={project.root}>
        {project.root}
      </div>
      <div className="onb-project-meta">
        库 {project.db_exists ? humanBytes(project.db_size_bytes) : '首次运行创建'}
      </div>
      {problems.map((text) => (
        <div key={text} className="onb-project-warn">
          {text}
        </div>
      ))}
      {!project.active && (
        <button
          type="button"
          className="secondary onb-switch"
          disabled={busy || !project.root_exists}
          title={project.root_exists ? '切换到这个工程（需要重启 API 才生效）' : '目录不存在'}
          onClick={() => onBind(project.root)}
        >
          切到这个工程
        </button>
      )}
    </li>
  );
}

export function ProjectOnboarding({ onBound }: { onBound?: () => void }) {
  const [data, setData] = useState<MountedProjectsResponse | null>(null);
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [showAll, setShowAll] = useState(false);

  const refresh = useCallback(() => {
    fetchProjects()
      .then((payload) => setData(payload))
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err));
      });
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onBind = useCallback(
    async (target: string) => {
      const value = target.trim();
      if (!value) {
        setError('请填一个工程目录（绝对路径）');
        return;
      }
      setBusy(true);
      setError('');
      setNotice('');
      try {
        const result = await bindProject(value);
        // 后端的 message 已经把"要不要重启""跨盘风险"说清楚了，原样显示。
        setNotice(result.message);
        setPath('');
        refresh();
        onBound?.();
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [onBound, refresh],
  );

  const bound = Boolean(data?.active_key);
  const projects = data?.projects ?? [];
  const active = projects.find((item) => item.active);
  const others = projects.filter((item) => !item.active);

  return (
    <section className={`onboarding${bound ? '' : ' onboarding-empty'}`} aria-label="工程绑定">
      <div className="onb-head">
        <h3>工程</h3>
        {bound ? (
          <span className="onb-badge onb-badge-ok">已绑定</span>
        ) : (
          <span className="onb-badge onb-badge-warn">未绑定</span>
        )}
      </div>

      {!data && !error && <p className="onb-hint">正在读取工程清单…</p>}

      {data && (
        <>
          {bound && active ? (
            <div className="onb-active">
              <div className="onb-active-root" title={active.root}>
                {active.root}
              </div>
              <div className="onb-project-meta">
                状态目录 {active.project_dir}
              </div>
            </div>
          ) : (
            <p className="onb-hint">
              还没有绑定工程。绑定之后任务的库、运行记录、工作树都会落在状态根里
              （状态根：{data.state_home}）。不绑也能用，但状态会退回仓库内的
              <code>workspace/</code>。
            </p>
          )}

          <label className="onb-field">
            <span className="onb-label">工程目录（绝对路径）</span>
            <input
              className="onb-input"
              value={path}
              placeholder="D:\work\my-game"
              spellCheck={false}
              disabled={busy}
              onChange={(event) => setPath(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void onBind(path);
              }}
            />
          </label>
          <div className="onb-actions">
            <button
              type="button"
              className="onb-bind"
              disabled={busy || !path.trim()}
              onClick={() => void onBind(path)}
            >
              {busy ? '绑定中…' : bound ? '绑定并切换' : '绑定这个工程'}
            </button>
            <button type="button" className="secondary" disabled={busy} onClick={refresh}>
              刷新
            </button>
          </div>

          {error && <p className="onb-error">{error}</p>}
          {notice && <p className="onb-notice">{notice}</p>}

          {others.length > 0 && (
            <details
              className="onb-others"
              open={showAll}
              onToggle={(event) => setShowAll((event.target as HTMLDetailsElement).open)}
            >
              <summary>其他已挂载工程（{others.length}）</summary>
              <ul className="onb-list">
                {others.map((item) => (
                  <ProjectRow key={item.key} project={item} busy={busy} onBind={onBind} />
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  );
}
