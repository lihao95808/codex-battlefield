/**
 * 洞察：结论、指标、投递、预授权、自动策略、成本、跨工程（P5-2：从 `api.ts` 拆出来）。
 *
 * `api.ts` 现在是聚合出口（re-export），老调用方不用改。
 */
import type {
  AutoPolicyReport,
  ConclusionDraft,
  ConclusionPayload,
  CostReport,
  CrossProjectResponse,
  DeliveryResponse,
  MetricsResponse,
  PreAuthorizationResponse,
} from './types';

import { readDetail } from './api-http';

/**
 * 生成结论文档草稿（P4-5）。**不落库** —— 草稿是给人改的中间产物。
 *
 * 事实由代码抽（影响面、决策点、代价、未清债），"当时为什么这么决定"留空由人补。
 */
export async function fetchConclusionDraft(rootId?: string, nodeId?: string): Promise<ConclusionDraft> {
  const params = new URLSearchParams();
  if (rootId) params.set('root_id', rootId);
  if (nodeId) params.set('node_id', nodeId);
  const query = params.toString() ? `?${params}` : '';
  const response = await fetch(`/api/conclusions/draft${query}`);
  if (!response.ok) {
    throw new Error(await readDetail(response, '生成草稿失败'));
  }
  return response.json();
}

/** 结论文档清单。 */
export async function fetchConclusions(rootId?: string): Promise<{ conclusions: ConclusionPayload[] }> {
  const query = rootId ? `?root_id=${encodeURIComponent(rootId)}` : '';
  const response = await fetch(`/api/conclusions${query}`);
  if (!response.ok) {
    throw new Error('结论清单读取失败：' + response.status);
  }
  return response.json();
}

/**
 * 落一份结论。**`authored` 必填** —— 后端会独立再校验一次。
 *
 * 写结论不受管家只读限制：只读禁的是"替用户动树"，归档文档不动任何东西。
 */
export async function saveConclusion(input: {
  rootId: string;
  authored: string;
  title?: string;
  nodeId?: string;
  author?: string;
  facts?: unknown;
  incomplete?: string[];
  status?: string;
  conclusionId?: number;
}): Promise<ConclusionPayload> {
  const response = await fetch('/api/conclusions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      root_id: input.rootId,
      authored: input.authored,
      title: input.title ?? '',
      node_id: input.nodeId ?? '',
      author: input.author ?? '',
      facts: input.facts ?? {},
      incomplete: input.incomplete ?? [],
      status: input.status ?? 'draft',
      conclusion_id: input.conclusionId,
    }),
  });
  if (!response.ok) {
    throw new Error(await readDetail(response, '保存结论失败'));
  }
  return response.json();
}

/** 导出链接（直接给 `<a href>` 用；返回的是 Markdown 附件）。 */
export function conclusionExportUrl(conclusionId: number): string {
  return `/api/conclusions/${conclusionId}/export`;
}

/**
 * 每个端点的耗时（P5-1 / P5-5）。
 *
 * `limit > 0` 时只给最慢的几个 —— 那是排查时真正要看的。
 * 分位数是**最近窗口**的，不是全历史（响应里的 `note` 也写了）。
 */
export async function fetchMetrics(limit = 10): Promise<MetricsResponse> {
  const response = await fetch(`/api/metrics?limit=${limit}`);
  if (!response.ok) {
    throw new Error('指标读取失败：' + response.status);
  }
  return response.json();
}

/** 把当前值定成某个端点的 p95 基线（P5-1）。 */
export async function setMetricsBaseline(key: string, p95Ms?: number): Promise<{ message: string }> {
  const response = await fetch('/api/metrics/baseline', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(p95Ms === undefined ? { key } : { key, p95_ms: p95Ms }),
  });
  if (!response.ok) {
    throw new Error(await readDetail(response, '写入基线失败'));
  }
  return response.json();
}

/**
 * 最近一次目标投递 + 它的**实时**进度（P4-1）。
 *
 * 回答"第几步、卡在哪"。没有投递过时 `available: false` + 原因 —— **不报 0/0**。
 */
export async function fetchDelivery(author?: string): Promise<DeliveryResponse> {
  const query = author ? `?author=${encodeURIComponent(author)}` : '';
  const response = await fetch(`/api/deliveries/latest${query}`);
  if (!response.ok) {
    throw new Error('投递进度读取失败：' + response.status);
  }
  return response.json();
}

/**
 * 预授权状态与动作风险目录（P4-4）。
 *
 * 目录一起拿回来是有意的：用户要先看到"哪些动作永远不能预授权"，才谈得上决定
 * 要不要开预授权。
 */
export async function fetchPreAuthorization(): Promise<PreAuthorizationResponse> {
  const response = await fetch('/api/steward/pre-authorization');
  if (!response.ok) {
    throw new Error('预授权状态读取失败：' + response.status);
  }
  return response.json();
}

/**
 * 授予一条预授权。**范围与期限都必填，且必须署名** —— 后端会独立再校验一次。
 *
 * 高风险动作会被后端 400 拒绝（界面也把它们标成不可勾选）：放宽权限是实质性的，
 * 不该只靠界面挡住。
 */
export async function grantPreAuthorization(input: {
  actions: string[];
  targets?: string[];
  minutes: number;
  grantedBy: string;
  reason?: string;
}): Promise<{ status: string; message: string }> {
  const response = await fetch('/api/steward/pre-authorization', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      actions: input.actions,
      targets: input.targets ?? [],
      minutes: input.minutes,
      granted_by: input.grantedBy,
      reason: input.reason ?? '',
    }),
  });
  if (!response.ok) {
    throw new Error(await readDetail(response, '授予预授权失败'));
  }
  return response.json();
}

/** 撤销预授权，立刻回到"每次都要人点"。 */
export async function revokePreAuthorization(): Promise<{ status: string; message: string }> {
  const response = await fetch('/api/steward/pre-authorization', { method: 'DELETE' });
  if (!response.ok) {
    throw new Error(await readDetail(response, '撤销预授权失败'));
  }
  return response.json();
}

/**
 * 自动策略的当前判定（P3-5）。
 *
 * **只判定、不执行**：动作由编排层做，界面只负责让"保护在不在"看得见。
 * 没有树时也会 200 返回（只报开关状态）—— 空工程上面板不该整个消失。
 */
export async function fetchAutoPolicy(rootId?: string): Promise<AutoPolicyReport> {
  const query = rootId ? `?root_id=${encodeURIComponent(rootId)}` : '';
  const response = await fetch(`/api/auto-policy${query}`);
  if (!response.ok) {
    throw new Error('自动策略读取失败：' + response.status);
  }
  return response.json();
}

/**
 * 花费归因与成本预测（P3-3 / P3-2）。
 *
 * `rootId` 省略时后端用最近更新的那棵树 —— 界面不必先问一次树。
 * 预测在样本不足时会带 `available: false` + 原因回来，**不是** 0。
 */
export async function fetchCost(rootId?: string): Promise<CostReport> {
  const query = rootId ? `?root_id=${encodeURIComponent(rootId)}` : '';
  const response = await fetch(`/api/cost${query}`);
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? '';
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || '花费读取失败：' + response.status);
  }
  return response.json();
}

/**
 * 跨工程总览（P3-4）。
 *
 * 进程同一时刻只连一个库，所以"我所有工程现在怎么样"只能靠后端跨库聚合。
 * 这个端点**只读且不建库**，读不到的工程会带 `available: false` + 原因回来
 * —— 界面必须把"读不到"和"零"分开显示。
 */
export async function fetchCrossProject(): Promise<CrossProjectResponse> {
  const response = await fetch('/api/overview/cross-project');
  if (!response.ok) {
    throw new Error('跨工程总览读取失败：' + response.status);
  }
  return response.json();
}
