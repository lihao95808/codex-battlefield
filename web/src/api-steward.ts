/**
 * 管家：对话、动作审批、告警流、注意账、档位（P5-2：从 `api.ts` 拆出来）。
 *
 * `api.ts` 现在是聚合出口（re-export），老调用方不用改。
 */
import type {
  AttentionResponse,
  StewardActionDecision,
  StewardActionsResponse,
  StewardAlertPush,
  StewardAlertsResponse,
  StewardChatResponse,
  StewardMessagesResponse,
  TuningChange,
  TuningResponse,
} from './types';

import { readDetail } from './api-http';

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
 * 当前的"注意"认领（P4-3）。
 *
 * 管家与看门狗都能发现问题，但谁也不知道对方已经看到了 —— 于是同一件事被处理两次。
 * 这张账是两边共用的：谁要处理谁记账，处理前先查账。
 */
export async function fetchAttention(): Promise<AttentionResponse> {
  const response = await fetch('/api/attention');
  if (!response.ok) {
    throw new Error('注意账读取失败：' + response.status);
  }
  return response.json();
}

/** 放掉一条认领。强制解除别人的必须留名（否则事后查不到是谁解的）。 */
export async function releaseAttention(input: {
  claimId: number;
  actor: string;
  force?: boolean;
  declaredBy?: string;
  reason?: string;
}): Promise<{ status: string; message: string }> {
  const params = new URLSearchParams({ actor: input.actor });
  if (input.force) params.set('force', 'true');
  if (input.declaredBy) params.set('declared_by', input.declaredBy);
  if (input.reason) params.set('reason', input.reason);
  const response = await fetch(`/api/attention/${input.claimId}?${params}`, { method: 'DELETE' });
  if (!response.ok) {
    throw new Error(await readDetail(response, '解除认领失败'));
  }
  return response.json();
}

/** 可调档位 + 当前值 + 调整历史（P4-2）。 */
export async function fetchTuning(): Promise<TuningResponse> {
  const response = await fetch('/api/watchdog/tuning');
  if (!response.ok) {
    throw new Error('档位读取失败：' + response.status);
  }
  return response.json();
}

/** 调整档位。**一批要么全过、要么全不过。** */
export async function applyTuning(input: {
  actor: string;
  changes: { key: string; value: unknown; reason?: string }[];
}): Promise<{ status: string; message: string; changes: TuningChange[] }> {
  const response = await fetch('/api/watchdog/tuning', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor: input.actor, changes: input.changes }),
  });
  if (!response.ok) {
    throw new Error(await readDetail(response, '调整档位失败'));
  }
  return response.json();
}

/** 回滚最近若干次档位调整。 */
export async function rollbackTuning(input: {
  actor: string;
  steps?: number;
}): Promise<{ status: string; message: string; steps: number }> {
  const response = await fetch('/api/watchdog/tuning/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor: input.actor, steps: input.steps ?? 1 }),
  });
  if (!response.ok) {
    throw new Error(await readDetail(response, '回滚失败'));
  }
  return response.json();
}
