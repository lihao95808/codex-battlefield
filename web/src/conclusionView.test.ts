/**
 * 结论文档面板显示逻辑的判据（P4-5）。
 *
 * 这一层要防的错只有一类：**让"还没写"看起来像"已经写完了"**。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | R1 | `authored` 为空时拒绝提交，并说清要写什么 | 归档一堆只有事实的"结论" |
 * | R2 | 草稿状态必须写明"没有落库" | 用户以为归档了，关掉页面就没了 |
 * | R3 | `incomplete` 为空时也给一句话 | 留空被读成"确定没有" |
 * | R4 | 状态有可读标签，且"草稿"与"定稿"分开 | 草稿被当成定论 |
 */
import { describe, expect, it } from 'vitest';

import {
  conclusionStatusLabel,
  draftStatusText,
  exportFileName,
  incompleteText,
  validateConclusion,
} from './conclusionView';
import type { ConclusionDraft, ConclusionPayload } from './types';

function draft(over: Partial<ConclusionDraft> = {}): ConclusionDraft {
  return {
    root_id: 'root-1',
    node_id: '',
    title: '一次卡住',
    facts: {
      node_count: 12,
      timeline: [
        { at: '2026-10-16T10:00:00', type: 'constitution.locked', message: '锁了', node_id: '' },
      ],
      timeline_total: 1,
    },
    incomplete: [],
    markdown: '# 一次卡住',
    note: '这是草稿、没有落库。',
    ...over,
  };
}

function conclusion(over: Partial<ConclusionPayload> = {}): ConclusionPayload {
  return {
    id: 7,
    root_id: 'root-1',
    node_id: '',
    title: '一次卡住',
    status: 'draft',
    author: 'duzuguang',
    authored: '我当时以为依赖已经装好了。',
    markdown: '# 一次卡住',
    facts: {},
    incomplete: [],
    created_at: null,
    updated_at: null,
    ...over,
  };
}

// ---------- R1 必须有人写的一段 ----------

describe('R1 没有"当时为什么"就不许提交', () => {
  it('空串、空白、纯换行都拒绝', () => {
    for (const value of ['', '   ', '\n\n', '\t']) {
      const problem = validateConclusion(value);
      expect(problem.field, JSON.stringify(value)).toBe('authored');
      expect(problem.message).toContain('事件表');
    }
  });

  it('有内容就放行', () => {
    const problem = validateConclusion('我当时以为依赖装好了。');
    expect(problem.field).toBeNull();
    expect(problem.message).toBe('');
  });
});

// ---------- R2 草稿没落库 ----------

describe('R2 草稿状态要写明没有落库', () => {
  it('状态文案里必须有"没有落库"', () => {
    expect(draftStatusText(draft())).toContain('没有落库');
  });

  it('给出节点数与决策点条数', () => {
    const text = draftStatusText(draft());
    expect(text).toContain('12 个节点');
    expect(text).toContain('1 条决策点');
  });

  it('时间线被截断时写明还有多少条未列出', () => {
    const text = draftStatusText(
      draft({ facts: { node_count: 3, timeline: [], timeline_total: 55 } }),
    );
    expect(text).toContain('55');
  });

  it('缺字段时不崩（facts 可能是空的）', () => {
    const text = draftStatusText(draft({ facts: {} }));
    expect(text).toContain('0 个节点');
  });
});

// ---------- R3 incomplete 为空也要有话说 ----------

describe('R3 没能确定的地方不能被留空', () => {
  it('有未确定项时逐条返回', () => {
    expect(incompleteText(draft({ incomplete: ['a', 'b'] }))).toEqual(['a', 'b']);
  });

  it('没有未确定项时给一句明确的话（而不是空数组）', () => {
    const items = incompleteText(draft({ incomplete: [] }));
    expect(items).toHaveLength(1);
    expect(items[0]).toContain('没有未确定项');
  });
});

// ---------- R4 状态标签 ----------

describe('R4 状态标签区分草稿与定稿', () => {
  it('草稿与定稿的文案不同，且草稿明说未定稿', () => {
    expect(conclusionStatusLabel('draft')).toContain('未定稿');
    expect(conclusionStatusLabel('final')).toContain('定稿');
    expect(conclusionStatusLabel('draft')).not.toBe(conclusionStatusLabel('final'));
  });

  it('未知状态原样带出（不假装是已知状态）', () => {
    expect(conclusionStatusLabel('published')).toContain('published');
  });
});

// ---------- 导出文件名 ----------

describe('导出文件名带 id', () => {
  it('用 id 区分多次归档', () => {
    expect(exportFileName(conclusion({ id: 42 }))).toBe('conclusion-42.md');
  });
});
