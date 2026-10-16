/**
 * 自动策略面板的**纯显示逻辑**（P3-5）。
 *
 * 这个面板最容易犯的错是**把三种状态混成两种**：
 *
 * | 状态 | 用户该做的事 |
 * |---|---|
 * | 已关闭 | 去设置里打开（他以为有保护，其实没有） |
 * | 已触发 | 看依据，确认动作对不对 |
 * | 开着没触发 | 什么都不用做 |
 *
 * 只显示"触发了/没触发"两态时，"已关闭"会伪装成"没到条件"—— 这正是
 * 这个面板存在的理由。所以 `policyRows()` 把 `disabled` 单列出来。
 */
import type { AutoPolicyItem, AutoPolicyReport, PolicyStatus } from './types';

/** 三态判定。**顺序有意义**：关闭优先于触发（关了就永远不会触发）。 */
export function policyStatus(item: AutoPolicyItem): PolicyStatus {
  if (!item.enabled) return 'disabled';
  return item.fired ? 'fired' : 'idle';
}

export const STATUS_LABEL: Record<PolicyStatus, string> = {
  disabled: '已关闭',
  fired: '已触发',
  idle: '守护中',
};

export interface PolicyRow {
  id: string;
  label: string;
  status: PolicyStatus;
  /** 一句话：为什么触发 / 为什么没触发 / 被谁关掉了。 */
  detail: string;
  /** 触发依据的可读形式（没触发时是当前观测值）。 */
  evidence: string;
  action: string;
}

/** 把证据对象打成一行可读文本。**只列基本类型**，嵌套结构原样 JSON 一下。 */
export function evidenceText(evidence: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(evidence)) {
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) {
      parts.push(`${key}: ${value.length ? value.join('、') : '（空）'}`);
    } else if (typeof value === 'object') {
      parts.push(`${key}: ${JSON.stringify(value)}`);
    } else {
      parts.push(`${key}: ${String(value)}`);
    }
  }
  return parts.join(' · ');
}

export function policyRows(report: AutoPolicyReport): PolicyRow[] {
  return report.policies.map((item) => ({
    id: item.id,
    label: item.label,
    status: policyStatus(item),
    detail: item.reason,
    evidence: evidenceText(item.evidence),
    action: item.action,
  }));
}

/**
 * 汇总一行。**必须显式说出关闭了几条** —— 那是"以为有保护"的信号，
 * 不能在汇总里被"4 条策略"这种中性描述盖过去。
 */
export function policySummary(report: AutoPolicyReport): string {
  const total = report.policies.length;
  const parts = [`${total} 条策略`, `已触发 ${report.fired_count}`];
  if (report.disabled.length > 0) {
    parts.push(`**已关闭 ${report.disabled.length}**`);
  }
  return parts.join(' · ');
}

/** 面板标题上的徽标文案：有触发或有关闭时都要显眼。 */
export function policyBadge(report: AutoPolicyReport): string {
  if (report.fired_count > 0) return `触发 ${report.fired_count}`;
  if (report.disabled.length > 0) return `关闭 ${report.disabled.length}`;
  return '全部守护中';
}
