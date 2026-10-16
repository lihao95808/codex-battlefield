/**
 * 目标投递显示逻辑的判据（P4-1）。
 *
 * 这一层要防的错只有一类：**把"读不到"渲染成一个看起来正常的数字**。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | D1 | `ratio` 为 `null` 时显示 `—`，**不显示 0%** | 0% 被读成"一点没做"，而真相可能是读错了树 |
 * | D2 | 进度行同时给分子与分母 | 只给百分比无法核对 |
 * | D3 | 有卡点时面板**默认展开** | 折叠起来的卡点等于没有卡点 |
 */
import { describe, expect, it } from 'vitest';

import { progressText, ratioText, shouldExpand } from './deliveryView';
import type { DeliveryProgress } from './types';

function progress(over: Partial<DeliveryProgress> = {}): DeliveryProgress {
  return {
    root_id: 'root-1',
    goal: '做个状态机',
    author: 'duzuguang',
    available: true,
    reason: '',
    done: 3,
    total: 10,
    ratio: 0.3,
    frontier: [],
    stuck: [],
    blocking_obligations: [],
    next_step: '正在推进最深的那一层。',
    incomplete: [],
    ...over,
  };
}

// ---------- D1 不把 null 渲染成 0% ----------

describe('D1 算不出来时给破折号，不给 0%', () => {
  it('null 与 undefined 都给 —', () => {
    expect(ratioText(null)).toBe('—');
    expect(ratioText(undefined)).toBe('—');
  });

  it('真的 0 才显示 0%', () => {
    expect(ratioText(0)).toBe('0%');
  });

  it('四舍五入到整数百分比', () => {
    expect(ratioText(0.333)).toBe('33%');
    expect(ratioText(1)).toBe('100%');
    expect(ratioText(0.5)).toBe('50%');
  });

  it('0 与 null 的结果必须不同（这是这一层存在的理由）', () => {
    expect(ratioText(0)).not.toBe(ratioText(null));
  });
});

// ---------- D2 分子分母 ----------

describe('D2 进度行必须给分子与分母', () => {
  it('两个数都在', () => {
    expect(progressText(progress({ done: 3, total: 10 }))).toContain('3');
    expect(progressText(progress({ done: 3, total: 10 }))).toContain('10');
  });

  it('0 总计时也照实显示（不隐藏）', () => {
    const text = progressText(progress({ done: 0, total: 0, ratio: null }));
    expect(text).toContain('0');
  });
});

// ---------- D3 卡点就展开 ----------

describe('D3 有卡点时默认展开', () => {
  it('有卡住节点 → 展开', () => {
    expect(shouldExpand(progress({ stuck: [{ id: 'b', title: 'B', status: 'blocked', error: '缺依赖' }] }))).toBe(true);
  });

  it('有阻断债 → 展开', () => {
    expect(
      shouldExpand(progress({ blocking_obligations: [{ id: 'ob', type: 'child_blocked', node_id: 'b' }] })),
    ).toBe(true);
  });

  it('顺利推进时不展开（默认收起，别占地方）', () => {
    expect(shouldExpand(progress())).toBe(false);
  });

  it('没有进度时不展开', () => {
    expect(shouldExpand(null)).toBe(false);
    expect(shouldExpand(undefined)).toBe(false);
  });
});
