/**
 * 花费面板显示逻辑的判据（P3-3 / P3-2）。
 *
 * 这一层最危险的错不是算错，而是**把"不知道"渲染成一个数字**：
 * 预测算不出来时显示 0、样本只有 1 个时显示一个精确的金额、
 * 漏掉的用量不提醒 —— 三条都会让用户对花费产生错误的确定感。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | Z1 | 预测显示**区间 + 样本量 + 依据** | 单一数字被当成承诺 |
 * | Z2 | 算不出来时显示**原因**，不显示 0 | 0 被读成"不用再花钱了" |
 * | Z3 | 样本 < 5 时给出"很粗"的显式提醒 | 3 个节点的均价被当成可靠外推 |
 * | Z4 | 缺用量 / 缺模型名必须显眼报出 | 归因表看起来完整，实际漏了一块 |
 * | Z5 | 金额按币种分行，不跨币种合计 | 加出来的数字在任何币种下都是错的 |
 * | Z6 | 归因按花费从大到小排 | 回答不了"钱主要花在哪" |
 */
import { describe, expect, it } from 'vitest';

import {
  WEAK_SAMPLE_NODES,
  amountText,
  attributionWarnings,
  bucketRow,
  projectionText,
  sortRowsByCost,
  topAmount,
} from './costView';
import type { CostBucket, CostProjection, CostReport } from './types';

function projection(over: Partial<CostProjection> = {}): CostProjection {
  return {
    available: true,
    reason: '',
    currency: 'USD',
    unit_cost: 0.5,
    settled_cost: 1.0,
    settled_nodes: 2,
    remaining_nodes: 4,
    low: 2,
    mid: 2.5,
    high: 5,
    sample_nodes: 8,
    retry_factor: 1.25,
    note: '',
    ...over,
  };
}

function bucket(key: string, over: Partial<CostBucket> = {}): CostBucket {
  return {
    key,
    runs: 1,
    total_tokens: 100,
    cost_by_currency: { USD: 1 },
    ...over,
  };
}

function report(over: Partial<CostReport['attribution']> = {}): CostReport {
  return {
    root_id: 'root',
    attribution: {
      runs: 3,
      runs_with_usage: 3,
      runs_without_usage: 0,
      runs_unknown_model: 0,
      total_tokens: 300,
      cost_by_currency: { USD: 3 },
      by_kind: [],
      by_model: [],
      by_node: [],
      ...over,
    },
    projection: projection(),
    node_counts: { total: 6, settled: 2, priced_samples: 2, remaining: 4 },
    note: '',
  };
}

// ---------- Z1 区间 + 样本量 ----------

describe('Z1 预测必须给区间与样本量', () => {
  it('区间用 en dash 连接上下界，不隐藏任何一端', () => {
    const text = projectionText(projection({ low: 2, high: 5 }));
    expect(text.range).toContain('–');
    expect(text.range).toContain('2.0000');
    expect(text.range).toContain('5.0000');
  });

  it('依据里必须出现样本量', () => {
    expect(projectionText(projection({ sample_nodes: 12 })).basis).toContain('12');
  });

  it('重试系数不等于 1 时显示出来，等于 1 时不显示噪音', () => {
    expect(projectionText(projection({ retry_factor: 1.25 })).basis).toContain('1.25');
    expect(projectionText(projection({ retry_factor: 1 })).basis).not.toContain('重试');
  });

  it('上下界相等时只给一个数（不给 "2.0000 – 2.0000"）', () => {
    const text = projectionText(projection({ low: 3, high: 3 }));
    expect(text.range).not.toContain('–');
    expect(text.range).toContain('3.0000');
  });

  it('没有币种时不假装有（显示"未标币种"）', () => {
    expect(projectionText(projection({ currency: null })).range).toContain('未标币种');
  });
});

// ---------- Z2 算不出来就说原因 ----------

describe('Z2 算不出来时显示原因而不是 0', () => {
  it('available=false 时给原因、不给区间', () => {
    const text = projectionText(
      projection({ available: false, reason: '还没有已结算的节点，无法外推。' }),
    );
    expect(text.available).toBe(false);
    expect(text.reason).toContain('还没有已结算的节点');
    expect(text.range).toBe('');
  });

  it('后端没给原因时也要有话说（不能渲染成空字符串）', () => {
    const text = projectionText(projection({ available: false, reason: '' }));
    expect(text.reason).toBeTruthy();
  });
});

// ---------- Z3 弱样本提醒 ----------

describe('Z3 样本太少要显式提醒', () => {
  it('样本少于阈值时 weakSample 为真', () => {
    expect(projectionText(projection({ sample_nodes: WEAK_SAMPLE_NODES - 1 })).weakSample).toBe(true);
    expect(projectionText(projection({ sample_nodes: WEAK_SAMPLE_NODES })).weakSample).toBe(false);
  });

  it('样本量为 1 时也算弱样本（最容易让人误信的情形）', () => {
    expect(projectionText(projection({ sample_nodes: 1 })).weakSample).toBe(true);
  });

  it('算不出来时不谈样本强弱（那是另一个问题）', () => {
    expect(projectionText(projection({ available: false })).weakSample).toBe(false);
  });
});

// ---------- Z4 缺数据提醒 ----------

describe('Z4 缺用量与缺模型名必须报出来', () => {
  it('没有可识别用量的运行会被点名，并说明金额不含它们', () => {
    const warnings = attributionWarnings(report({ runs: 4, runs_with_usage: 2, runs_without_usage: 2 }));
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('2 次运行没有可识别的用量数据');
    expect(warnings[0]).toContain('不含');
  });

  it('没有模型名的运行会被点名，并说明该表不含它们但合计仍含', () => {
    const warnings = attributionWarnings(report({ runs_unknown_model: 3 }));
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('没有记录模型名');
    expect(warnings[0]).toContain('合计');
  });

  it('两种情况都有时给两条提醒（不合并成一条含糊的话）', () => {
    const warnings = attributionWarnings(
      report({ runs: 5, runs_with_usage: 3, runs_without_usage: 2, runs_unknown_model: 1 }),
    );
    expect(warnings).toHaveLength(2);
  });

  it('数据齐全时不给任何提醒（没有假警）', () => {
    expect(attributionWarnings(report())).toEqual([]);
  });
});

// ---------- Z5 币种 ----------

describe('Z5 金额按币种分行', () => {
  it('多币种给多行并按币种码排序', () => {
    const row = bucketRow(bucket('n', { cost_by_currency: { USD: 2, CNY: 1 } }));
    expect(row.costs).toEqual([
      { code: 'CNY', amount: 1 },
      { code: 'USD', amount: 2 },
    ]);
  });

  it('没有金额时给空数组（界面据此显示"无金额"，而不是 0）', () => {
    expect(bucketRow(bucket('n', { cost_by_currency: null })).costs).toEqual([]);
  });

  it('金额小于 0.01 时多留几位，否则显示成 0.00', () => {
    expect(amountText('USD', 0.0004)).toBe('USD 0.000400');
    expect(amountText('USD', 1.5)).toBe('USD 1.5000');
  });
});

// ---------- Z6 排序 ----------

describe('Z6 归因按花费从大到小', () => {
  it('金额大的排前面', () => {
    const rows = sortRowsByCost([
      bucketRow(bucket('cheap', { cost_by_currency: { USD: 1 } })),
      bucketRow(bucket('pricey', { cost_by_currency: { USD: 9 } })),
    ]);
    expect(rows.map((row) => row.key)).toEqual(['pricey', 'cheap']);
  });

  it('金额相同时按 token 多的排前面', () => {
    const rows = sortRowsByCost([
      bucketRow(bucket('few', { total_tokens: 10, cost_by_currency: { USD: 1 } })),
      bucketRow(bucket('many', { total_tokens: 900, cost_by_currency: { USD: 1 } })),
    ]);
    expect(rows.map((row) => row.key)).toEqual(['many', 'few']);
  });

  it('没有金额的排最后，且不改动入参顺序', () => {
    const original = [
      bucketRow(bucket('no-money', { cost_by_currency: null })),
      bucketRow(bucket('has-money', { cost_by_currency: { USD: 1 } })),
    ];
    expect(sortRowsByCost(original).map((row) => row.key)).toEqual(['has-money', 'no-money']);
    expect(original.map((row) => row.key)).toEqual(['no-money', 'has-money']);
  });

  it('topAmount 取行内最大金额（多币种时用于排序）', () => {
    expect(topAmount(bucketRow(bucket('n', { cost_by_currency: { CNY: 1, USD: 7 } })))).toBe(7);
    expect(topAmount(bucketRow(bucket('n', { cost_by_currency: null })))).toBe(0);
  });
});
