import type {
  ApiRestartDrainResponse,
  BudgetGuardResponse,
  ConstitutionResponse,
  ObligationIndexBoard,
  RunArtifactResponse,
  BindProjectResponse,
  MountedProjectsResponse,
  StateLayoutResponse,
  StewardActionDecision,
  StewardActionsResponse,
  StewardAlertPush,
  StewardAlertsResponse,
  StewardChatResponse,
  StewardMessagesResponse,
  TaskDetailResponse,
  TreeResponse,
  WatchdogActionResponse,
  WatchdogStateResponse,
} from './types';

/**
 * 管家（steward）的对话接口。
 *
 * 历史由**后端**保存（`steward_messages` 表），不是浏览器本地：
 * 刷新/换浏览器都还在，而且可审计（谁说了什么、当时看到哪份快照）。
 */
export async function fetchStewardMessages(): Promise<StewardMessagesResponse> {
  const response = await fetch('/api/steward/messages');
  if (!response.ok) {
    throw new Error(`管家对话读取失败：${response.status}`);
  }
  return response.json();
}

export async function sendStewardMessage(message: string): Promise<StewardChatResponse> {
  const response = await fetch('/api/steward/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) {
    throw new Error(`管家没有回应：${response.status}`);
  }
  return response.json();
}

/** 清空对话。**只清对话**，不动任务树 —— 措辞上也要让用户确信这一点。 */
export async function clearStewardMessages(): Promise<{ status: string; removed: number }> {
  const response = await fetch('/api/steward/messages', { method: 'DELETE' });
  if (!response.ok) {
    throw new Error(`清空对话失败：${response.status}`);
  }
  return response.json();
}

/**
 * 管家提议的动作（含各自的审批/执行足迹）。
 *
 * 管家**永远不能自己执行** —— 它提议、你批准、服务端执行。
 * 所以这条列表是"等你决定"的收件箱，不是"它做过什么"的日志。
 */
export async function fetchStewardActions(status = ''): Promise<StewardActionsResponse> {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  const response = await fetch(`/api/steward/actions${query}`);
  if (!response.ok) {
    throw new Error(`动作列表读取失败：${response.status}`);
  }
  return response.json();
}

/** 批准并执行。返回 `ok: false` 表示**执行失败**（批准过但没做成），不是请求失败。 */
export async function approveStewardAction(actionId: number): Promise<StewardActionDecision> {
  const response = await fetch(`/api/steward/actions/${actionId}/approve`, { method: 'POST' });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`批准失败：${response.status} ${detail.slice(0, 160)}`);
  }
  return response.json();
}

export async function rejectStewardAction(actionId: number): Promise<StewardActionDecision> {
  const response = await fetch(`/api/steward/actions/${actionId}/reject`, { method: 'POST' });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`拒绝失败：${response.status} ${detail.slice(0, 160)}`);
  }
  return response.json();
}

/** 值得打扰用户的信号。由服务端规则判定 —— 便宜、确定、可测。 */
export async function fetchStewardAlerts(): Promise<StewardAlertsResponse> {
  const response = await fetch('/api/steward/alerts');
  if (!response.ok) {
    throw new Error(`提醒读取失败：${response.status}`);
  }
  return response.json();
}

/**
 * 订阅**主动推送**的提醒（SSE）。
 *
 * 为什么不用轮询：轮询要么慢（间隔大，用户等）要么浪费（间隔小，绝大多数
 * 请求什么都没变）。服务端做的是**边沿触发**——只在提醒集合或等级真的
 * 变化时说话——所以客户端可以安心地一直挂着连接。
 *
 * 返回一个取消函数（组件卸载时调用）。`EventSource` 自带断线重连，
 * 所以这里不自己实现退避；只在**永久失败**（服务端明确关闭）时报告。
 */
export function subscribeStewardAlerts(
  onPush: (push: StewardAlertPush) => void,
  onError?: (message: string) => void,
): () => void {
  // 相对路径 —— 跟着页面走，因此 Vite 的 /api 代理和同源部署都能用。
  const source = new EventSource('/api/steward/alerts/stream');

  source.addEventListener('alerts', (event) => {
    try {
      onPush(JSON.parse((event as MessageEvent).data) as StewardAlertPush);
    } catch (error) {
      onError?.(`推送解析失败：${error instanceof Error ? error.message : String(error)}`);
    }
  });

  // 服务端评估失败时会发这个（读库出错等）。流不会因此断掉。
  source.addEventListener('error', (event) => {
    const data = (event as MessageEvent).data;
    if (typeof data === 'string' && data) {
      try {
        const parsed = JSON.parse(data) as { message?: string };
        onError?.(parsed.message ?? '推送流报告了错误。');
        return;
      } catch {
        // 落到下面的通用分支
      }
    }
    // 没有 data 的 error 事件是**连接层**的（断线/重连中）——
    // EventSource 自己会重连，不该当成错误弹给用户，否则界面会一直闪。
  });

  return () => source.close();
}

/**
 * 当前状态布局：在哪个工程、用哪个库、状态根在哪。
 *
 * 绑不绑工程都要能拿到（未绑定时后端只回机器级字段并带 `bound: false`），
 * 所以不把它当成"可能失败的附加信息"——它是诊断入口。
 */
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

// 跨树债务聚合（**只读**派生索引）。返回里带 `built_at` 与 `in_sync`：缓存过期时
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
