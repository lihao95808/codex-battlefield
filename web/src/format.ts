/**
 * 界面用的**格式化**函数（从 `App.tsx` 拆出，P5-2）。
 *
 * 这些都是纯函数，值得单独放一处并各自有判据 —— 它们的共同危险是
 * **把"没有"和"零"显示成同一个东西**（`0` 与 `未排程` 与 `n/a`），
 * 而那种混同会让用户按错的方向去查问题。
 */

/** 里程碑数字：拿不到就给 `fallback`（默认 0）。 */
export function statNumber(
  stats: Record<string, unknown> | null | undefined,
  key: string,
  fallback = 0,
): number {
  const value = stats?.[key];
  return typeof value === 'number' ? value : fallback;
}

/** 里程碑文本：拿不到就给 `fallback`（默认 `默认`）。 */
export function statText(
  stats: Record<string, unknown> | null | undefined,
  key: string,
  fallback = '默认',
): string {
  const value = stats?.[key];
  return typeof value === 'string' ? value : fallback;
}

/**
 * 槽位显示数：**不会超过容量**。
 *
 * 为什么需要夹一下：`active` 来自进程扫描、`capacity` 来自配置，
 * 两者短暂不一致是正常的（刚回收完/刚扩容）。显示成"5/4"会让人以为有 bug，
 * 而实际只是采样时刻不同。
 */
export function displaySlotCount(activeSlots: number, capacity: number): number {
  const safeActive = Math.max(0, activeSlots);
  const safeCapacity = Math.max(0, capacity);
  return safeCapacity > 0 ? Math.min(safeActive, safeCapacity) : safeActive;
}

/** 日期时间。空值给 `未排程`，解不出来**原样返回**（能看到它到底是什么）。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '未排程';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** 距离某个时间点还有多少秒。解不出来给 `null`（**不是 0**）。 */
export function secondsUntil(value: string | null | undefined): number | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return Math.max(0, Math.ceil((date.getTime() - Date.now()) / 1000));
}

/** 时长（秒）→ 人话。`null` / `undefined` 给 `未排程`（**不是 `0 分钟`**）。 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '未排程';
  const safeSeconds = Math.max(0, seconds);
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  if (hours > 0) return `${hours} 小时 ${minutes} 分钟`;
  return `${minutes} 分钟`;
}

/** 比例 → 百分比。非数字给 `n/a`（**不是 `0%`**），并夹到 0–100。 */
export function formatPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return 'n/a';
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

/** token 数 → `12.3k` / `1.2M`。 */
export function formatTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

export interface UsageLike {
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_input_tokens?: number | null;
  total_tokens?: number | null;
  runs?: number | null;
  cost_by_currency?: Record<string, number> | null;
}

/** 用量摘要。**完全没有用量时给 `null`**（界面据此不渲染，而不是显示"用量 0"）。 */
export function formatUsageSummary(usage: UsageLike | null | undefined): string | null {
  if (!usage) return null;
  const input = usage.input_tokens ?? 0;
  const output = usage.output_tokens ?? 0;
  const cached = usage.cached_input_tokens ?? 0;
  if (!input && !output && !(usage.total_tokens ?? 0)) return null;
  const parts = [`入 ${formatTokenCount(input)}`, `出 ${formatTokenCount(output)}`];
  if (input > 0 && cached > 0) parts.push(`缓存 ${Math.round((cached / input) * 100)}%`);
  if (usage.runs) parts.push(`${usage.runs} 次调用`);
  return `用量 ${parts.join(' · ')}`;
}

/**
 * 花费摘要。**按币种分别显示，不相加** —— 与后端 `usage.summarize_usages` 同一口径。
 * 一个跨币种的总额在任何币种下都是错的。
 */
export function formatCostSummary(usage: UsageLike | null | undefined): string | null {
  const buckets = usage?.cost_by_currency;
  if (!buckets) return null;
  const entries = Object.entries(buckets).filter(
    ([, amount]) => typeof amount === 'number' && amount > 0,
  );
  if (!entries.length) return null;
  const symbols: Record<string, string> = { CNY: '¥', USD: '$' };
  return entries
    .map(([code, amount]) => `${symbols[code] ?? `${code} `}${amount.toFixed(4)}`)
    .join(' / ');
}

/** 事件时钟（`HH:MM:SS`）。解不出来给空串（时间列留白比显示 `Invalid Date` 好）。 */
export function formatEventClock(createdAt: string): string {
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

/** 看门狗动作结果 → 一句话。`exit_code` 为空时说"无退出码"，不假装是 0。 */
export function summarizeWatchdogAction(result: {
  stdout?: string;
  stderr?: string;
  status: string;
  exit_code: number | null;
}): string {
  const text = [result.stdout, result.stderr].join('\n').trim();
  const lastLine = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(-1)[0];
  const label = result.status === 'succeeded' ? '成功' : '失败';
  const code = result.exit_code === null ? '无退出码' : `退出码 ${result.exit_code}`;
  return `${label}，${code}${lastLine ? `：${lastLine}` : ''}`;
}
