/**
 * 目标投递面板的**纯显示逻辑**（P4-1）。
 *
 * 这一层要防的错只有一类：**把"读不到"渲染成一个看起来正常的数字**。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | D1 | `ratio` 为 `null` 时显示 `—`，**不显示 0%** | 0% 被读成"一点没做" |
 * | D2 | 进度行必须同时给分子与分母 | 只给百分比无法核对 |
 * | D3 | 卡点/阻断债优先于进度出现在"现在该看什么"里 | 用户以为一切正常，直到很久后才发现有人在等 |
 */
import type { DeliveryProgress } from './types';

/**
 * `0.5` → `50%`；`null` / `undefined` → `—`。
 *
 * **绝不把 `null` 渲染成 `0%`**：分母为 0 时后端给 `null`（"算不出来"），
 * 而 `0%` 会被读成"一点没做" —— 两件事对用户的含义完全不同。
 */
export function ratioText(ratio: number | null | undefined): string {
  if (typeof ratio !== 'number') return '—';
  return `${Math.round(ratio * 100)}%`;
}

/** 进度行文本。**分子分母都要给** —— 只给百分比无法核对。 */
export function progressText(progress: DeliveryProgress): string {
  return `已完成 ${progress.done} / 总计 ${progress.total}`;
}

/**
 * 面板该不该默认展开。
 *
 * **有卡点就展开**：折叠起来的卡点等于没有卡点 —— 而这一栏存在的理由正是
 * "别让用户在很久之后才发现有人在等"。
 */
export function shouldExpand(progress: DeliveryProgress | null | undefined): boolean {
  if (!progress) return false;
  return progress.stuck.length > 0 || progress.blocking_obligations.length > 0;
}
