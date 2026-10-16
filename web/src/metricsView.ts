/**
 * 端点耗时面板的**纯显示逻辑**（P5-1 / P5-5）。
 *
 * 这一层要防的错只有一类：**把"还没量到"渲染成"很快"**。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | P1 | `null` 的耗时显示 `—`，**不显示 `0ms`** | 0ms 看起来像"快得看不见"，真相是"还没量到" |
 * | P2 | `null` 的错误率显示 `—`，**不显示 `0%`** | 0% 读成"很稳"，而真相是"没有样本" |
 * | P3 | 退化行排在**最前面**并标出 | 埋在长列表里的退化等于没报 |
 */
import type { MetricsResponse, RouteMetrics } from './types';

/** `12.34` → `12.3ms`；`null` / `undefined` → `—`。 */
export function msText(value: number | null | undefined): string {
  if (typeof value !== 'number') return '—';
  return `${value.toFixed(1)}ms`;
}

/** 错误率百分比。没有样本给 `—`，**不给 0%**。 */
export function errorRateText(value: number | null | undefined): string {
  if (typeof value !== 'number') return '—';
  return `${(value * 100).toFixed(1)}%`;
}

/**
 * 表格行的顺序：**退化的排最前**，其余按 p95 从慢到快。
 *
 * 后端已经按 p95 排好了，这里只把退化的提到最前 —— 因为"比上周慢了 5 倍"
 * 比"本来就慢"更值得先看，而它可能 p95 并不高（比如 20ms → 100ms）。
 */
export function orderedRoutes(data: MetricsResponse): RouteMetrics[] {
  const regressed = new Map(data.regressions.map((item) => [item.key, item.ratio]));
  const flagged = data.routes.filter((route) => regressed.has(route.key));
  const rest = data.routes.filter((route) => !regressed.has(route.key));
  flagged.sort((a, b) => (regressed.get(b.key) ?? 0) - (regressed.get(a.key) ?? 0));
  return [...flagged, ...rest];
}

/** 标题徽标：有退化就报退化数，否则报端点数。 */
export function badgeText(data: MetricsResponse): string {
  if (data.regressions.length > 0) return `${data.regressions.length} 个退化`;
  return `${data.route_count} 个端点`;
}
