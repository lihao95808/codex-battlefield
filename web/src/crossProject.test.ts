/**
 * 跨工程总览显示逻辑的判据（P3-4）。
 *
 * 这些判断都属"**显示错了比不显示更糟**"，所以必须能被直接调用并断言，
 * 而不是埋在 JSX 里靠人肉 review：
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | Y1 | `null`（读不到）与 `0`（真的是零）显示**不同** | "还没跑过"被显示成"工程是空的" |
 * | Y2 | 排序 = 需要人动手的程度 | 用户得自己判断哪一行更急，看板失去意义 |
 * | Y3 | 金额**按币种分开**，绝不跨币种合计 | 加出来的数字在任何币种下都是错的 |
 * | Y4 | 合计偏小时**必须说出来** | 少算的合计给人"我还早"的错觉 |
 * | Y5 | 金额很小时保留足够位数 | 0.0004 显示成 `0.00`，看起来像没花钱 |
 */
import { describe, expect, it } from 'vitest';

import {
  VERDICT_LABEL,
  VERDICT_ORDER,
  countText,
  costLines,
  humanBytes,
  moneyText,
  sortByVerdict,
  tokenText,
  totalsText,
} from './crossProject';
import type { CrossProjectEntry, CrossProjectTotals, CrossProjectVerdict } from './types';

function entry(key: string, verdict: CrossProjectVerdict, active = false): CrossProjectEntry {
  return {
    key,
    root: `D:\\work\\${key}`,
    active,
    root_exists: true,
    db_bytes: 1024,
    state_bytes: 2048,
    available: verdict !== 'unknown',
    reason: verdict === 'unknown' ? '还没跑过（没有数据库文件）' : '',
    task_total: verdict === 'unknown' ? null : 3,
    task_by_status: verdict === 'unknown' ? {} : { done: 3 },
    roots: [],
    spend: null,
    open_blocking_obligations: verdict === 'unknown' ? null : verdict === 'blocked' ? 2 : 0,
    latest_event: null,
    lead_goal: null,
    verdict,
  };
}

function totals(over: Partial<CrossProjectTotals> = {}): CrossProjectTotals {
  return {
    project_count: 3,
    readable_count: 3,
    task_total: 9,
    open_blocking_obligations: 0,
    total_tokens: 1200,
    cost_by_currency: null,
    cost_unlabeled: null,
    blocked_projects: [],
    unreadable_projects: [],
    totals_incomplete: false,
    ...over,
  };
}

// ---------- Y1 读不到 ≠ 0 ----------

describe('Y1 读不到与零必须显示得不一样', () => {
  it('countText 对 null 说"读不到"，对 0 说 "0"', () => {
    expect(countText(null)).toBe('读不到');
    expect(countText(undefined)).toBe('读不到');
    expect(countText(0)).toBe('0');
    expect(countText(7)).toBe('7');
  });

  it('tokenText 同样区分（null 不给 0）', () => {
    expect(tokenText(null)).toBe('读不到');
    expect(tokenText(0)).toBe('0');
  });

  it('tokenText 把人话做大数缩写', () => {
    expect(tokenText(999)).toBe('999');
    expect(tokenText(1500)).toBe('1.5k');
    expect(tokenText(2_500_000)).toBe('2.50M');
  });

  it('humanBytes 处理 0 与单位', () => {
    expect(humanBytes(0)).toBe('0 B');
    expect(humanBytes(512)).toBe('512 B');
    expect(humanBytes(2048)).toBe('2 KB');
    expect(humanBytes(5 * 1024 * 1024)).toBe('5.0 MB');
  });
});

// ---------- Y2 排序 ----------

describe('Y2 排序按"需要人动手"的程度', () => {
  it('顺序是 读不到 > 等你处理 > 在跑 > 空闲', () => {
    expect(VERDICT_ORDER.unknown).toBeLessThan(VERDICT_ORDER.blocked);
    expect(VERDICT_ORDER.blocked).toBeLessThan(VERDICT_ORDER.running);
    expect(VERDICT_ORDER.running).toBeLessThan(VERDICT_ORDER.idle);
  });

  it('每种结论都有中文说明（不能漏）', () => {
    for (const verdict of ['unknown', 'blocked', 'running', 'idle'] as CrossProjectVerdict[]) {
      expect(VERDICT_LABEL[verdict]).toBeTruthy();
    }
    // "unknown" 的文案必须是"读不到"，不是"未知" —— 它要指向"你去看看为什么"。
    expect(VERDICT_LABEL.unknown).toBe('读不到');
  });

  it('排完序后，需要动手的排在最前', () => {
    const sorted = sortByVerdict([
      entry('idle-1', 'idle'),
      entry('running-1', 'running'),
      entry('broken-1', 'unknown'),
      entry('blocked-1', 'blocked'),
    ]);
    expect(sorted.map((item) => item.key)).toEqual([
      'broken-1',
      'blocked-1',
      'running-1',
      'idle-1',
    ]);
  });

  it('同一档内当前工程排前面，且不改动入参顺序', () => {
    const original = [entry('a', 'running'), entry('b', 'running', true)];
    const sorted = sortByVerdict(original);
    expect(sorted.map((item) => item.key)).toEqual(['b', 'a']);
    expect(original.map((item) => item.key)).toEqual(['a', 'b']);
  });
});

// ---------- Y3 按币种分开 ----------

describe('Y3 金额按币种分开，不跨币种合计', () => {
  it('多币种给多行，且按币种码排序', () => {
    const lines = costLines(totals({ cost_by_currency: { USD: 2, CNY: 1.5 } }));
    expect(lines).toEqual([
      { code: 'CNY', amount: 1.5 },
      { code: 'USD', amount: 2 },
    ]);
  });

  it('没有币种标签的金额单列一行', () => {
    const lines = costLines(totals({ cost_by_currency: { USD: 1 }, cost_unlabeled: 0.5 }));
    expect(lines.map((line) => line.code)).toEqual(['USD', '（未标币种）']);
  });

  it('完全没有计价数据时给空数组（界面据此显示"没有可用的计价数据"而不是 0）', () => {
    expect(costLines(totals())).toEqual([]);
  });

  it('无标签金额为 0 时不产生多余的一行', () => {
    expect(costLines(totals({ cost_unlabeled: 0 }))).toEqual([]);
  });
});

// ---------- Y4 合计偏小要说出来 ----------

describe('Y4 合计偏小时必须说出来', () => {
  it('完整时只报数字', () => {
    const text = totalsText(totals());
    expect(text).toContain('3/3 个工程可读');
    expect(text).not.toContain('不含');
  });

  it('有工程读不到时点名"合计不含 N 个"', () => {
    const text = totalsText(
      totals({
        project_count: 4,
        readable_count: 3,
        totals_incomplete: true,
        unreadable_projects: ['x'],
      }),
    );
    expect(text).toContain('3/4 个工程可读');
    expect(text).toContain('合计不含 1 个读不到的工程');
  });
});

// ---------- Y5 小金额的位数 ----------

describe('Y5 小金额保留足够位数', () => {
  it('小于 0.01 的金额保留 6 位（否则看起来像 0）', () => {
    expect(moneyText({ code: 'USD', amount: 0.0004 })).toBe('USD 0.000400');
  });

  it('普通金额保留 4 位', () => {
    expect(moneyText({ code: 'CNY', amount: 1.5 })).toBe('CNY 1.5000');
  });

  it('零也是 4 位（不特殊处理成 6 位）', () => {
    expect(moneyText({ code: 'USD', amount: 0 })).toBe('USD 0.0000');
  });
});
