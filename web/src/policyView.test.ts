/**
 * 自动策略显示逻辑的判据（P3-5）。
 *
 * 这个面板唯一会犯的严重错误是**把"已关闭"混进"守护中"**：
 * 前者意味着用户以为有保护其实没有，而两者的界面表现原本可以完全一样。
 * 所以下面的判据大半在钉这个区分。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | P1 | 三态：已关闭 / 已触发 / 守护中，且**关闭优先** | "关掉了"伪装成"没到条件" |
 * | P2 | 汇总必须显式说出关闭了几条 | 中性描述盖过"以为有保护"这个信号 |
 * | P3 | 徽标优先报触发、其次报关闭 | 面板标题看不出有异常 |
 * | P4 | 证据要打成可读一行，空/嵌套都不能崩 | 触发依据看不见，事后无法复盘 |
 * | P5 | 每行都带动作名 | 用户不知道"触发了"意味着机器要做什么 |
 */
import { describe, expect, it } from 'vitest';

import { STATUS_LABEL, evidenceText, policyBadge, policyRows, policyStatus, policySummary } from './policyView';
import type { AutoPolicyItem, AutoPolicyReport } from './types';

function item(over: Partial<AutoPolicyItem> = {}): AutoPolicyItem {
  return {
    id: 'stop_on_consecutive_failure',
    label: '连续失败自动停',
    rationale: '同一个节点反复失败几乎总是信息缺失。',
    switch: 'auto_stop_on_consecutive_failure',
    enabled: true,
    action: 'stop_retrying',
    fired: false,
    reason: '没有节点连续失败到 3 次',
    evidence: { limit: 3, worst_streak: 1 },
    ...over,
  };
}

function report(items: AutoPolicyItem[]): AutoPolicyReport {
  return {
    policies: items,
    fired_count: items.filter((entry) => entry.fired && entry.enabled).length,
    fired: items.filter((entry) => entry.fired && entry.enabled).map((entry) => entry.id),
    disabled: items.filter((entry) => !entry.enabled).map((entry) => entry.id),
    note: '',
  };
}

// ---------- P1 三态 ----------

describe('P1 三态且关闭优先', () => {
  it('关闭的策略报 disabled，即使它本来会触发', () => {
    expect(policyStatus(item({ enabled: false, fired: true }))).toBe('disabled');
  });

  it('开着且触发报 fired', () => {
    expect(policyStatus(item({ fired: true }))).toBe('fired');
  });

  it('开着没触发报 idle', () => {
    expect(policyStatus(item())).toBe('idle');
  });

  it('三种状态各有中文标签（不能共用一种说法）', () => {
    expect(STATUS_LABEL.disabled).toBe('已关闭');
    expect(STATUS_LABEL.fired).toBe('已触发');
    expect(STATUS_LABEL.idle).toBe('守护中');
    expect(new Set(Object.values(STATUS_LABEL)).size).toBe(3);
  });

  it('行里保留后端给的理由（不能改写成中性文案）', () => {
    const rows = policyRows(report([item({ reason: '策略已关闭（策略开关 x 之前不判定）' })]));
    expect(rows[0].detail).toContain('策略已关闭');
  });
});

// ---------- P2 汇总 ----------

describe('P2 汇总必须说出关闭了几条', () => {
  it('有关闭时汇总里出现"已关闭 N"', () => {
    const text = policySummary(
      report([item({ id: 'a' }), item({ id: 'b', enabled: false, switch: 's2' })]),
    );
    expect(text).toContain('2 条策略');
    expect(text).toContain('已关闭 1');
  });

  it('没有关闭时不提关闭（但也不假装一切正常）', () => {
    const text = policySummary(report([item()]));
    expect(text).not.toContain('已关闭');
    expect(text).toContain('已触发 0');
  });
});

// ---------- P3 徽标 ----------

describe('P3 徽标优先报触发、其次报关闭', () => {
  it('有触发时报触发数', () => {
    expect(policyBadge(report([item({ fired: true })]))).toBe('触发 1');
  });

  it('没触发但有关闭时报关闭数', () => {
    expect(policyBadge(report([item({ enabled: false })]))).toBe('关闭 1');
  });

  it('都没有时报"全部守护中"', () => {
    expect(policyBadge(report([item()]))).toBe('全部守护中');
  });

  it('关闭的策略即使 fired 标记为真也不计入触发数', () => {
    // 后端在关闭时不会给 fired=True，但界面不该依赖这一点。
    const built = report([item({ enabled: false, fired: true })]);
    expect(policyBadge(built)).toBe('关闭 1');
  });
});

// ---------- P4 证据 ----------

describe('P4 证据打成可读一行', () => {
  it('基本类型按 key: value 拼接', () => {
    expect(evidenceText({ limit: 3, worst_streak: 1 })).toBe('limit: 3 · worst_streak: 1');
  });

  it('数组列出元素，空数组给"（空）"而不是空白', () => {
    expect(evidenceText({ nodes: ['a', 'b'] })).toBe('nodes: a、b');
    expect(evidenceText({ nodes: [] })).toBe('nodes: （空）');
  });

  it('嵌套对象退化成 JSON，不崩也不吞', () => {
    expect(evidenceText({ nested: { a: 1 } })).toBe('nested: {"a":1}');
  });

  it('null/undefined 跳过（不渲染成 "null"）', () => {
    expect(evidenceText({ a: null, b: undefined, c: 1 })).toBe('c: 1');
  });

  it('空对象给空串（面板据此不渲染那一行）', () => {
    expect(evidenceText({})).toBe('');
  });
});

// ---------- P5 动作名 ----------

describe('P5 每行都带动作名', () => {
  it('动作名原样带出，供用户知道机器要做什么', () => {
    const rows = policyRows(report([item({ action: 'reclaim_slots' })]));
    expect(rows[0].action).toBe('reclaim_slots');
  });
});
