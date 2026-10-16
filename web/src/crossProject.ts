/**
 * 跨工程总览的**纯显示逻辑**（P3-4）。
 *
 * 单独一个模块，不是因为"组件里放不下"，而是因为这里有几条**必须能被测试钉住**的
 * 判断 —— 它们都属于"显示错了比不显示更糟"那一类：
 *
 * 1. `verdict` 的优先级与文案：哪个工程需要人动手，看板必须一眼答对。
 * 2. **`null` 与 `0` 绝不混用**：读不到时后端给 `null`，界面必须显示"读不到"，
 *    显示成 `0` 会让用户以为那个工程是空的。
 * 3. 金额**按币种分别显示**，不做跨币种合计。
 *
 * 组件只负责摆版式；判断在这里，于是可以被 test 直接调用。
 */
import type { CrossProjectEntry, CrossProjectTotals, CrossProjectVerdict } from './types';

/** 字节数转人话。 */
export function humanBytes(value: number): string {
  if (!value) return '0 B';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 每个结论的中文说明。**说清"要不要我动手"**，不只说状态。 */
export const VERDICT_LABEL: Record<CrossProjectVerdict, string> = {
  unknown: '读不到',
  blocked: '等你处理',
  running: '在跑',
  idle: '空闲',
};

/** 排列顺序 = 需要人动手的程度。看板按它排序，用户从上往下看即可。 */
export const VERDICT_ORDER: Record<CrossProjectVerdict, number> = {
  unknown: 0,
  blocked: 1,
  running: 2,
  idle: 3,
};

/**
 * 可空的计数：`null` 显示"读不到"，`0` 显示 `0`。
 *
 * 这条看着琐碎，但它是本模块存在的主要理由：后端**刻意**用 `null` 表示
 * "读不到"、用 `0` 表示"真的是零"。界面一旦把两者都渲染成 `0`（`value ?? 0`
 * 这种顺手写法），"这个工程还没跑过"就变成了"这个工程是空的"。
 */
export function countText(value: number | null | undefined): string {
  return typeof value === 'number' ? String(value) : '读不到';
}

/** 代币数转人话（`null` → "读不到"）。 */
export function tokenText(value: number | null | undefined): string {
  if (typeof value !== 'number') return '读不到';
  if (value === 0) return '0';
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1_000_000).toFixed(2)}M`;
}

export interface CostLine {
  code: string;
  amount: number;
}

/**
 * 金额拆成若干行：**每个币种一行**，无标签的单列一行。
 *
 * 不做跨币种合计 —— 把 CNY 与 USD 加起来的数字在任何币种下都是错的。
 */
export function costLines(totals: CrossProjectTotals): CostLine[] {
  const lines: CostLine[] = Object.entries(totals.cost_by_currency ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([code, amount]) => ({ code, amount }));
  if (typeof totals.cost_unlabeled === 'number' && totals.cost_unlabeled > 0) {
    lines.push({ code: '（未标币种）', amount: totals.cost_unlabeled });
  }
  return lines;
}

/** 金额显示：币种 + 保留 4 位有效小数（成本常常很小，2 位会显示成 0.00）。 */
export function moneyText(line: CostLine): string {
  const digits = line.amount !== 0 && Math.abs(line.amount) < 0.01 ? 6 : 4;
  return `${line.code} ${line.amount.toFixed(digits)}`;
}

/** 按"需要人动手"排序，同一档内保持后端的顺序（active 在前）。 */
export function sortByVerdict(projects: CrossProjectEntry[]): CrossProjectEntry[] {
  return [...projects].sort((a, b) => {
    const delta = VERDICT_ORDER[a.verdict] - VERDICT_ORDER[b.verdict];
    if (delta !== 0) return delta;
    return Number(b.active) - Number(a.active);
  });
}

/**
 * 合计那一行的文字。
 *
 * `totals_incomplete` 为真时必须带出"不含 N 个工程" —— 少算的合计算起来
 * 比不显示更危险：它会给人"我还早"的错觉。
 */
export function totalsText(totals: CrossProjectTotals): string {
  const parts = [
    `${totals.readable_count}/${totals.project_count} 个工程可读`,
    `任务 ${totals.task_total}`,
    `未清阻断债 ${totals.open_blocking_obligations}`,
    `token ${tokenText(totals.total_tokens)}`,
  ];
  if (totals.totals_incomplete) {
    parts.push(`（合计不含 ${totals.unreadable_projects.length} 个读不到的工程）`);
  }
  return parts.join(' · ');
}
