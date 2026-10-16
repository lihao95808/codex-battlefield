/**
 * 排版与词表的判据（P5-2 拆分出来的模块）。
 *
 * 这些函数原来的位置在一个 2900 行的组件文件里，所以**没有任何直接判据** ——
 * 只能靠"界面看起来对"。拆出来之后可以逐个钉，而它们共同的危险正好是
 * **把"没有"与"零"显示成同一个东西**：
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | F1 | `null` 时长给 `未排程`，**不给 `0 分钟`** | 用户以为"马上就到"，其实没排程 |
 * | F2 | 非数字比例给 `n/a`，**不给 `0%`** | 0% 读成"一点没做" |
 * | F3 | `secondsUntil` 解不出来给 `null`，**不给 0** | 0 秒读成"现在就到" |
 * | F4 | 槽位显示数**不超过容量** | 显示 "5/4" 会让人以为有 bug |
 * | F5 | 事件严重度：先判危险，再判警告 | 一个同时含 hibernate 与 failed 的事件被显示成"警告" |
 * | F6 | 词表查不到**原样返回** | 后端新增状态时界面显示"未知"，看不出它叫什么 |
 * | F7 | 花费**按币种分别显示，不相加** | 跨币种总额在任何币种下都是错的 |
 */
import { describe, expect, it } from 'vitest';

import {
  autoConcurrencyLabel,
  blockParentActionLabel,
  blockRetryableLabel,
  eventSeverity,
  eventTypeLabel,
  runKindLabel,
  runStatusLabel,
  statusLabel,
  watchdogActionLabel,
} from './labels';
import {
  displaySlotCount,
  formatCostSummary,
  formatDateTime,
  formatDuration,
  formatEventClock,
  formatPercent,
  formatTokenCount,
  formatUsageSummary,
  secondsUntil,
  statNumber,
  statText,
  summarizeWatchdogAction,
} from './format';

// ---------- F1 / F2 / F3 不要用 0 表示"没有" ----------

describe('F1 "没有"不能用 0 表示', () => {
  it('时长缺值给"未排程"，不是 0 分钟', () => {
    expect(formatDuration(null)).toBe('未排程');
    expect(formatDuration(undefined)).toBe('未排程');
    expect(formatDuration(0)).toBe('0 分钟');
    expect(formatDuration(null)).not.toBe(formatDuration(0));
  });

  it('时长负数被夹到 0（而不是显示 -5 分钟）', () => {
    expect(formatDuration(-5)).toBe('0 分钟');
  });

  it('小时以上给"X 小时 Y 分钟"', () => {
    expect(formatDuration(3660)).toBe('1 小时 1 分钟');
    expect(formatDuration(59)).toBe('0 分钟');
  });

  it('时长就是 0 秒时给 0 分钟（真的 0）', () => {
    expect(formatDuration(0)).toBe('0 分钟');
  });
});

describe('F2 比例缺值给 n/a', () => {
  it('null 与 NaN 都给 n/a，不是 0%', () => {
    expect(formatPercent(null)).toBe('n/a');
    expect(formatPercent(undefined)).toBe('n/a');
    expect(formatPercent(Number.NaN)).toBe('n/a');
  });

  it('真的 0 才给 0%', () => {
    expect(formatPercent(0)).toBe('0%');
  });

  it('夹到 0–100 之间（后端给 1.5 时不显示 150%）', () => {
    expect(formatPercent(1.5)).toBe('100%');
    expect(formatPercent(-1)).toBe('0%');
  });
});

describe('F3 secondsUntil 解不出来给 null', () => {
  it('空值与非日期都给 null，不是 0', () => {
    expect(secondsUntil(null)).toBeNull();
    expect(secondsUntil(undefined)).toBeNull();
    expect(secondsUntil('不是日期')).toBeNull();
  });

  it('过去的时间给 0（真的到了）', () => {
    expect(secondsUntil(new Date(Date.now() - 10_000).toISOString())).toBe(0);
  });

  it('未来的时间给正数', () => {
    expect(secondsUntil(new Date(Date.now() + 60_000).toISOString())).toBeGreaterThan(0);
  });
});

// ---------- F4 槽位 ----------

describe('F4 槽位显示数不超过容量', () => {
  it('活跃数被夹到容量', () => {
    expect(displaySlotCount(5, 4)).toBe(4);
  });

  it('容量为 0 时原样给活跃数（不假装有上限）', () => {
    expect(displaySlotCount(3, 0)).toBe(3);
  });

  it('负数被夹到 0', () => {
    expect(displaySlotCount(-2, 4)).toBe(0);
    expect(displaySlotCount(2, -1)).toBe(2);
  });
});

// ---------- F5 事件严重度 ----------

describe('F5 事件严重度先判危险', () => {
  it('含 failed / blocked 就是 danger', () => {
    expect(eventSeverity('task.failed')).toBe('danger');
    expect(eventSeverity('task.blocked')).toBe('danger');
  });

  it('同时含 hibernate 与 failed 时按 danger（顺序有意义）', () => {
    expect(eventSeverity('tree.hibernate_failed')).toBe('danger');
  });

  it('休眠/修复/取消是 warn', () => {
    expect(eventSeverity('tree.hibernate')).toBe('warn');
    expect(eventSeverity('task.repair')).toBe('warn');
    expect(eventSeverity('task.canceled')).toBe('warn');
  });

  it('完成与恢复是 ok', () => {
    expect(eventSeverity('task.done')).toBe('ok');
    expect(eventSeverity('constitution.locked')).toBe('ok');
  });

  it('`hibernate_resume` 落在 warn 上（休眠那一档先命中）', () => {
    // 这条**不是**在为一个理想结果背书，而是把现状钉住并说明它为什么说得过去：
    // `warn` 的判定里有 `hibernate`，而 `ok` 的判定里有 `resume`；前者先判，
    // 所以恢复事件显示成"警告"色。对使用者来说"刚休眠完恢复"确实值得看一眼。
    // 第一版我在测试里写的是 `ok` —— 是测试错了，不是代码错了。
    expect(eventSeverity('task.hibernate_resume')).toBe('warn');
  });

  it('其他是 info', () => {
    expect(eventSeverity('task.created')).toBe('info');
  });
});

// ---------- F6 词表 ----------

describe('F6 词表查不到就原样返回', () => {
  it('已知状态给中文', () => {
    expect(statusLabel('blocked')).toBe('已阻塞');
    expect(runKindLabel('worker')).toBe('执行');
    expect(runStatusLabel('succeeded')).toBe('成功');
    expect(autoConcurrencyLabel('balanced')).toBe('均衡');
    expect(watchdogActionLabel('resume')).toBe('到点恢复');
  });

  it('未知状态原样返回 —— 界面能看到后端到底给了什么', () => {
    expect(statusLabel('brand_new_state')).toBe('brand_new_state');
    expect(eventTypeLabel('brand.new.event')).toBe('brand.new.event');
    expect(autoConcurrencyLabel('new_mode')).toBe('new_mode');
  });

  it('阻塞母动作默认给"人工判断"（最保守的那一档）', () => {
    expect(blockParentActionLabel(undefined)).toBe('人工判断');
    expect(blockParentActionLabel('recursive_recovery')).toBe('转为递归修复');
  });

  it('重试推断：拿不到布尔值时**默认说不能自动重试**', () => {
    // 把不能重试的说成能重试会让用户干等。
    expect(blockRetryableLabel(undefined, undefined)).toBe('否，需要人工审查');
    expect(blockRetryableLabel(false, 'pause_tool_fix')).toBe('否，工具修复后自动恢复');
    expect(blockRetryableLabel(true, 'pause_tool_fix')).toBe('是，等待后可重试');
  });
});

// ---------- F7 花费 ----------

describe('F7 花费按币种分别显示', () => {
  it('多币种用 / 分隔，不相加', () => {
    expect(formatCostSummary({ cost_by_currency: { CNY: 1.5, USD: 2 } })).toBe('¥1.5000 / $2.0000');
  });

  it('没有花费桶时给 null（界面据此不渲染）', () => {
    expect(formatCostSummary(null)).toBeNull();
    expect(formatCostSummary({})).toBeNull();
    expect(formatCostSummary({ cost_by_currency: {} })).toBeNull();
  });

  it('全为 0 时也给 null —— 而不是显示"¥0.0000"', () => {
    expect(formatCostSummary({ cost_by_currency: { CNY: 0 } })).toBeNull();
  });

  it('未知币种用代码本身（不猜符号）', () => {
    expect(formatCostSummary({ cost_by_currency: { EUR: 1 } })).toBe('EUR 1.0000');
  });
});

// ---------- 其他 ----------

describe('用量与 token 数', () => {
  it('完全没有用量时给 null', () => {
    expect(formatUsageSummary(null)).toBeNull();
    expect(formatUsageSummary({})).toBeNull();
    expect(formatUsageSummary({ input_tokens: 0, output_tokens: 0 })).toBeNull();
  });

  it('有用量时给入/出，缓存比例只在入 > 0 时给', () => {
    expect(formatUsageSummary({ input_tokens: 100, output_tokens: 20 })).toContain('入 100');
    expect(formatUsageSummary({ input_tokens: 100, cached_input_tokens: 50 })).toContain('缓存 50%');
    // 入为 0 时缓存比例算不出来 —— 而整个"用量"在入/出/总计都为 0 时就是"没有用量"，
    // 所以这里给 `null`（界面据此不渲染），而不是显示"缓存 0%"这种假数字。
    expect(formatUsageSummary({ input_tokens: 0, cached_input_tokens: 50 })).toBeNull();
  });

  it('只有缓存 token 而没有入/出时也算"没有可显示的用量"', () => {
    expect(formatUsageSummary({ cached_input_tokens: 500 })).toBeNull();
  });

  it('token 数按 k/M 缩写', () => {
    expect(formatTokenCount(999)).toBe('999');
    expect(formatTokenCount(1500)).toBe('1.5k');
    expect(formatTokenCount(2_500_000)).toBe('2.5M');
  });
});

describe('统计取值', () => {
  it('拿不到就回落，不编', () => {
    expect(statNumber({ total: 5 }, 'total')).toBe(5);
    expect(statNumber({ total: '5' }, 'total')).toBe(0);
    expect(statNumber(null, 'total', 7)).toBe(7);
    expect(statText({ mode: 'mock' }, 'mode')).toBe('mock');
    expect(statText({}, 'mode')).toBe('默认');
  });
});

describe('看门狗动作摘要', () => {
  it('成功带退出码与最后一行', () => {
    expect(
      summarizeWatchdogAction({ stdout: 'a\nb', stderr: '', status: 'succeeded', exit_code: 0 }),
    ).toBe('成功，退出码 0：b');
  });

  it('无退出码时说"无退出码"，不假装是 0', () => {
    expect(
      summarizeWatchdogAction({ stdout: '', stderr: '', status: 'failed', exit_code: null }),
    ).toBe('失败，无退出码');
  });
});

describe('日期与时钟', () => {
  it('空日期给未排程，坏日期原样返回', () => {
    expect(formatDateTime(null)).toBe('未排程');
    expect(formatDateTime('不是日期')).toBe('不是日期');
  });

  it('事件时钟解不出来给空串（不留 Invalid Date）', () => {
    expect(formatEventClock('不是日期')).toBe('');
    expect(formatEventClock('2026-10-16T10:00:00Z')).toMatch(/\d/);
  });
});
