/**
 * 任务树：起树、宪法、看门狗、节点动作（P5-2：从 `api.ts` 拆出来）。
 *
 * `api.ts` 现在是聚合出口（re-export），老调用方不用改。
 */
import type {
  ApiRestartDrainResponse,
  BudgetGuardResponse,
  ConstitutionResponse,
  ObligationIndexBoard,
  RunArtifactResponse,
  TaskDetailResponse,
  TreeResponse,
  WatchdogActionResponse,
  WatchdogStateResponse,
} from './types';

export async function fetchTree(
  rootId?: string | null,
  options: { compactDone?: boolean; expandedDoneParentIds?: string[] } = {},
): Promise<TreeResponse> {
  const params = new URLSearchParams();
  if (rootId) {
    params.set('root_id', rootId);
  }
  if (options.compactDone !== undefined) {
    params.set('compact_done', options.compactDone ? 'true' : 'false');
  }
  for (const id of options.expandedDoneParentIds ?? []) {
    params.append('expanded_done_parent_id', id);
  }
  const query = params.toString() ? `?${params.toString()}` : '';
  const response = await fetch(`/api/tree${query}`);
  if (!response.ok) {
    throw new Error(`任务树加载失败：${response.status}`);
  }
  return response.json();
}

// `total` 不构成读数，界面必须自己把这句话说出来，不能只显示一个 0。
export async function fetchObligationIndex(status: 'open' | 'settled' | 'all' = 'open'): Promise<ObligationIndexBoard> {
  const response = await fetch(`/api/obligation-index?status=${status}`);
  if (!response.ok) {
    throw new Error(`跨树债务索引加载失败：${response.status}`);
  }
  return response.json();
}


export async function fetchWatchdogState(rootId?: string | null): Promise<WatchdogStateResponse> {
  const query = rootId ? `?root_id=${encodeURIComponent(rootId)}` : '';
  const response = await fetch(`/api/watchdog/state${query}`);
  if (!response.ok) {
    throw new Error(`自动续跑状态加载失败：${response.status}`);
  }
  return response.json();
}


export async function installWatchdog(rootId?: string | null): Promise<WatchdogActionResponse> {
  const query = rootId ? `?root_id=${encodeURIComponent(rootId)}` : '';
  const response = await fetch(`/api/watchdog/install${query}`, { method: 'POST' });
  if (!response.ok) {
    throw new Error(`自启动钩子安装失败：${response.status}`);
  }
  return response.json();
}


export async function runWatchdogOnce(rootId?: string | null): Promise<WatchdogActionResponse> {
  const query = rootId ? `?root_id=${encodeURIComponent(rootId)}` : '';
  const response = await fetch(`/api/watchdog/run-once${query}`, { method: 'POST' });
  if (!response.ok) {
    throw new Error(`watchdog 巡检失败：${response.status}`);
  }
  return response.json();
}


export async function setApiRestartDrain(armed: boolean): Promise<ApiRestartDrainResponse> {
  const response = await fetch('/api/watchdog/api-restart-drain', { method: armed ? 'POST' : 'DELETE' });
  if (!response.ok) {
    throw new Error(`维护排空切换失败：${response.status}`);
  }
  return response.json();
}


export async function fetchTask(taskId: string): Promise<TaskDetailResponse> {
  const response = await fetch(`/api/tasks/${taskId}`);
  if (!response.ok) {
    throw new Error(`任务详情加载失败：${response.status}`);
  }
  return response.json();
}


export async function fetchRunArtifact(runId: number, kind: 'prompt' | 'logs' | 'last_message'): Promise<RunArtifactResponse> {
  const response = await fetch(`/api/runs/${runId}/artifact?kind=${kind}`);
  if (!response.ok) {
    throw new Error(`运行调试文件加载失败：${response.status}`);
  }
  return response.json();
}

// 界面会据此把树误判成"宪法陈旧"。
export async function fetchLatestConstitution(rootId?: string | null): Promise<ConstitutionResponse | null> {
  const query = rootId ? `?root_id=${encodeURIComponent(rootId)}` : '';
  const response = await fetch(`/api/constitutions/latest${query}`);
  if (!response.ok) {
    throw new Error(`项目宪法加载失败：${response.status}`);
  }
  return response.json();
}


export async function startProject(
  goal: string,
  runnerMode: 'mock' | 'real',
  targetPath: string | null,
  constitutionId: string,
): Promise<{ root_id: string; status: string }> {
  const response = await fetch('/api/projects/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ goal, runner_mode: runnerMode, target_path: targetPath || null, constitution_id: constitutionId, auto_run: true }),
  });
  if (!response.ok) {
    throw new Error(`项目启动失败：${response.status}`);
  }
  return response.json();
}


export async function startOneShotProject(goal: string): Promise<{ root_id: string; status: string }> {
  const response = await fetch('/api/projects/one-shot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ goal }),
  });
  if (!response.ok) {
    throw new Error(`一键启动失败：${response.status}`);
  }
  return response.json();
}


export async function draftConstitution(goal: string, runnerMode: 'mock' | 'real', targetPath: string | null): Promise<ConstitutionResponse> {
  const response = await fetch('/api/constitutions/draft', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ goal, runner_mode: runnerMode, target_path: targetPath || null }),
  });
  if (!response.ok) {
    throw new Error(`项目宪法生成失败：${response.status}`);
  }
  return response.json();
}


export async function updateConstitution(
  constitutionId: string,
  markdown: string,
  data: Record<string, unknown>,
): Promise<ConstitutionResponse> {
  const response = await fetch(`/api/constitutions/${constitutionId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ markdown, data }),
  });
  if (!response.ok) {
    throw new Error(`项目宪法保存失败：${response.status}`);
  }
  return response.json();
}


export async function lockConstitution(constitutionId: string): Promise<ConstitutionResponse> {
  const response = await fetch(`/api/constitutions/${constitutionId}/lock`, { method: 'POST' });
  if (!response.ok) {
    throw new Error(`项目宪法锁定失败：${response.status}`);
  }
  return response.json();
}


export async function postTaskAction(taskId: string, action: 'retry' | 'pause' | 'resume' | 'cancel'): Promise<void> {
  const response = await fetch(`/api/tasks/${taskId}/${action}`, { method: 'POST' });
  if (!response.ok) {
    throw new Error(`任务操作失败（${action}）：${response.status}`);
  }
}


export async function hibernateTask(taskId: string, resumeAfterSeconds = 18_000): Promise<{ status: string; count: number; resume_at: string }> {
  const response = await fetch(`/api/tasks/${taskId}/hibernate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ resume_after_seconds: resumeAfterSeconds, reason: '等待 Codex/token 池刷新后自动恢复' }),
  });
  if (!response.ok) {
    throw new Error(`任务树休眠失败：${response.status}`);
  }
  return response.json();
}


export async function setBudgetGuard(
  rootId: string,
  resumeAfterSeconds = 18_000,
  reason = '余额不足，保存任务树状态并低功耗等待账户额度刷新。',
): Promise<BudgetGuardResponse> {
  const response = await fetch('/api/budget-guard', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      root_id: rootId,
      resume_after_seconds: resumeAfterSeconds,
      reason,
      hibernate_tree: true,
    }),
  });
  if (!response.ok) {
    throw new Error(`预算守卫设置失败：${response.status}`);
  }
  return response.json();
}


export async function cancelAllProjects(rootId?: string | null): Promise<{ status: string; count: number }> {
  const query = rootId ? `?root_id=${encodeURIComponent(rootId)}` : '';
  const response = await fetch(`/api/projects/cancel-all${query}`, { method: 'POST' });
  if (!response.ok) {
    throw new Error(`停止任务树失败：${response.status}`);
  }
  return response.json();
}
