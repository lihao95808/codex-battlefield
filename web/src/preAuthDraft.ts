/**
 * 预授权面板的**纯显示逻辑**（P4-4）。
 *
 * 这一层要防的错只有一类：**界面让"放宽权限"看起来比实际更小**。
 * 具体是三种表现，各有一条判据：
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | Q1 | 高风险动作**永远不可勾选**，且要显示出来（不是藏起来） | 藏起来会让人以为"没列出来 = 也能预授权" |
 * | Q2 | 三档都有可读标签 | 只写 "high" 不解释为什么 |
 * | Q3 | 期限与署名缺失时给**具体**提示 | 笼统"请填写"让人不知道缺什么 |
 * | Q4 | 授权生效中要显示剩余时间 | 一条快过期的授权看起来和刚授的一样 |
 */
import type { PreAuthCatalogItem, PreAuthGrant } from './types';

export type RiskLevel = 'low' | 'medium' | 'high';

/** 排序权重：危险的在前面（与后端目录同序，界面再排一次以防 API 变化）。 */
export const RISK_ORDER: Record<RiskLevel, number> = { high: 0, medium: 1, low: 2 };

/**
 * 列表里显示的风险文案。
 *
 * **高风险必须明确写出"永远需要你确认"**：只显示风险等级会让人以为
 * 攒够权限就能勾上它。
 */
export function riskLabel(item: PreAuthCatalogItem): string {
  if (item.risk === 'high') return '高风险 · 永远需要你确认';
  if (item.risk === 'medium') return '中风险 · 可预授权（会留痕）';
  return '低风险 · 可预授权';
}

/** 能勾的动作（= 后端说可以预授权的那些）。 */
export function selectableActions(catalog: PreAuthCatalogItem[]): PreAuthCatalogItem[] {
  return catalog.filter((item) => item.pre_authorizable);
}

/**
 * 切换勾选。**高风险动作一律不进入选中集**。
 *
 * 双保险：界面上 checkbox 已经 `disabled`，这里再挡一次 —— 后者防的是
 * "以后有人把 disabled 去掉"（那种改动看起来只是样式问题）。
 */
export function toggleAction(
  selected: string[],
  action: string,
  preAuthorizable: boolean,
): string[] {
  if (!preAuthorizable) return selected;
  return selected.includes(action)
    ? selected.filter((item) => item !== action)
    : [...selected, action];
}

export interface GrantProblem {
  field: 'actions' | 'minutes' | 'signature' | null;
  message: string;
}

/**
 * 提交前的本地校验。给出**具体缺什么**，而不是笼统一句"请填写完整"。
 *
 * 后端会独立再校验一次（它是权威），这里只是让用户少跑一趟。
 */
export function validateDraft(draft: {
  actions: string[];
  minutes: string;
  signature: string;
}): GrantProblem {
  if (draft.actions.length === 0) {
    return { field: 'actions', message: '至少要勾一个动作。' };
  }
  const window = Number(draft.minutes);
  if (!Number.isFinite(window) || window <= 0) {
    return { field: 'minutes', message: '期限必须是正的分钟数 —— 没有无限期的预授权。' };
  }
  if (!draft.signature.trim()) {
    return { field: 'signature', message: '要署名：事后必须能回答"这条授权是谁给的"。' };
  }
  return { field: null, message: '' };
}

/** 剩余时间的可读形式。**过期了要明确说过期**，不能只显示 `0s`。 */
export function remainingText(grant: PreAuthGrant): string {
  if (grant.remaining_seconds <= 0) return '已过期（下次执行需要你确认）';
  const seconds = grant.remaining_seconds;
  if (seconds >= 3600) return `还剩 ${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
  if (seconds >= 60) return `还剩 ${Math.floor(seconds / 60)} 分`;
  return `还剩 ${seconds} 秒`;
}

/** 授权生效中要显示的警告（没有授权时给空串，界面据此不渲染）。 */
export function grantWarning(grant: PreAuthGrant): string {
  if (grant.remaining_seconds <= 0) {
    return '这条预授权已过期，之后每个动作都需要你确认（撤销它只是清理记录）。';
  }
  const scope = grant.targets.length ? `仅限目标 ${grant.targets.join('、')}` : '不限目标';
  return `生效中：${scope}。高风险动作不在此范围内。`;
}
