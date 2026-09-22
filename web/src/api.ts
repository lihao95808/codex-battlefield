import type {
  ApiRestartDrainResponse,
  BudgetGuardResponse,
  ConstitutionResponse,
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

// 不传 rootId 时后端只在**不歧义**的情况下给答案：多棵树并存会 409。
// 多树是常态时必须在树已确定之后带上 rootId，否则读到的是"最近更新的那条"，
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
