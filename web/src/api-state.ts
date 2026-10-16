/**
 * 状态：运行时设置、库迁移/重置、工程绑定（P5-2：从 `api.ts` 拆出来）。
 *
 * `api.ts` 现在是聚合出口（re-export），老调用方不用改。
 */
import type {
  BindProjectResponse,
  MigrationApplyResponse,
  MigrationPreviewResponse,
  MountedProjectsResponse,
  SettingsResponse,
  SettingsSaveResponse,
  StateLayoutResponse,
  StateOverviewResponse,
  StateResetResponse,
} from './types';

/**
 * 当前状态布局：在哪个工程、用哪个库、状态根在哪。
 *
 * 绑不绑工程都要能拿到（未绑定时后端只回机器级字段并带 `bound: false`），
 * 所以不把它当成"可能失败的附加信息"——它是诊断入口。
 */
/**
 * 可编辑设置的登记表 + 当前生效值 + 每项来自哪里（P2-2）。
 *
 * 后端会一并回报来源：被环境变量压着的项在界面上改**不会有效果**，
 * 这一点必须让用户看见，否则他会反复保存、反复重启、反复疑惑。
 */
export async function fetchSettings(): Promise<SettingsResponse> {
  const response = await fetch('/api/settings');
  if (!response.ok) {
    throw new Error('设置读取失败：' + response.status);
  }
  return response.json();
}

/**
 * 保存设置到用户配置文件。
 *
 * confirmRisky 对应后端的二次确认：危险项（放宽沙箱、打开主动汇报…）
 * 不带它会被 409 拒绝 —— 那道闸防的是点一下下拉框就生效。
 */
export async function saveSettings(
  values: Record<string, string>,
  confirmRisky: boolean,
): Promise<SettingsSaveResponse> {
  const response = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values, confirm_risky: confirmRisky }),
  });
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? '';
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || '保存失败：' + response.status);
  }
  return response.json();
}

/**
 * 状态根与各工程的只读概览（P2-5）。
 *
 * 回答三个原先只能靠命令行的问题：状态落在哪、每个工程占多大、
 * 旧 `<repo>/workspace/` 里还剩什么需要迁移。
 */
export async function fetchStateOverview(): Promise<StateOverviewResponse> {
  const response = await fetch('/api/state/overview');
  if (!response.ok) {
    throw new Error('状态概览读取失败：' + response.status);
  }
  return response.json();
}

/**
 * 迁移**预演**（`dryRun: true`）或真正执行。
 *
 * 预演不写任何文件，用来让用户在动手前看到目标 key、旧库大小与节点数、
 * 以及每个候选工程能不能用 —— "目标选错了"是这里唯一会造成损失的错。
 */
export async function migrateState(
  projectRoot: string,
  dryRun: boolean,
): Promise<MigrationPreviewResponse | MigrationApplyResponse> {
  const response = await fetch('/api/state/migrate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_root: projectRoot, dry_run: dryRun }),
  });
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? '';
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || '迁移失败：' + response.status);
  }
  return response.json();
}

/**
 * 清空当前工程的状态目录（**不可逆**）。
 *
 * `confirm` 必须逐字等于工程 key —— 后端会核对，不匹配就 409。
 * 这不是仪式：删状态没有后悔药，要求把 key 抄一遍是让手滑付出代价。
 */
export async function resetProjectState(confirm: string): Promise<StateResetResponse> {
  const response = await fetch('/api/state/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm, purge: true }),
  });
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? '';
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || '重置失败：' + response.status);
  }
  return response.json();
}


export async function fetchStateLayout(): Promise<StateLayoutResponse> {
  const response = await fetch('/api/state');
  if (!response.ok) {
    throw new Error(`状态布局读取失败：${response.status}`);
  }
  return response.json();
}

/**
 * 已挂载的工程清单 + 当前是哪个。
 *
 * 未绑定工程时界面上原来只有一行小字，用户不知道下一步做什么。这份清单让
 * "我现在在哪 / 有哪些可选 / 怎么绑" 都能在界面里回答，不必回到命令行。
 */
export async function fetchProjects(): Promise<MountedProjectsResponse> {
  const response = await fetch('/api/projects');
  if (!response.ok) {
    throw new Error(`工程清单读取失败：${response.status}`);
  }
  return response.json();
}

/**
 * 绑定（挂载）一个工程目录并设为当前。
 *
 * 注意返回值里的 `restartRequired`：绑定写的是 `active.json`，而**当前进程**的
 * 库连接与工作目录是启动时定下的。所以"绑好了"和"生效了"是两件事，
 * 界面必须把这一点说出来，否则用户会以为点了就切过去了。
 */
export async function bindProject(path: string): Promise<BindProjectResponse> {
  const response = await fetch('/api/projects/bind', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!response.ok) {
    // 后端把拒绝原因写在 detail 里（目录不存在、状态根当工程、跨盘…），
    // 那是给用户看的，原样带上来比"绑定失败：400"有用得多。
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? '';
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || `绑定失败：${response.status}`);
  }
  return response.json();
}
