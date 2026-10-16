/**
 * 预授权面板显示逻辑的判据（P4-4）。
 *
 * 这一层要防的错只有一类：**界面让"放宽权限"看起来比实际更小**。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | Q1 | 高风险动作**永远不可勾选**，且要显示出来（不是藏起来） | 藏起来会让人以为"没列出来 = 也能预授权" |
 * | Q2 | 三档都有可读标签且写明"高风险永远要确认" | 只写 "high" 不解释 |
 * | Q3 | 缺动作/期限/署名时给**具体**提示 | 笼统"请填写"让人不知道缺什么 |
 * | Q4 | 有效期与过期要分开显示 | 一条快过期的授权看起来和刚授的一样 |
 */
import { describe, expect, it } from 'vitest';

import {
  RISK_ORDER,
  grantWarning,
  remainingText,
  riskLabel,
  selectableActions,
  toggleAction,
  validateDraft,
} from './preAuthDraft';
import type { PreAuthCatalogItem, PreAuthGrant } from './types';

function item(over: Partial<PreAuthCatalogItem> = {}): PreAuthCatalogItem {
  return {
    action: 'kick_root',
    risk: 'low',
    label: '低',
    rationale: '可逆',
    pre_authorizable: true,
    ...over,
  };
}

function grant(over: Partial<PreAuthGrant> = {}): PreAuthGrant {
  return {
    actions: ['kick_root'],
    targets: [],
    expires_at: '2026-10-16T12:00:00+00:00',
    remaining_seconds: 600,
    granted_by: 'me',
    reason: 'r',
    ...over,
  };
}

// ---------- Q1 高风险不可勾 ----------

describe('Q1 高风险动作永远不可勾选', () => {
  it('toggleAction 拒绝把高风险动作加进选中集', () => {
    expect(toggleAction([], 'cancel_all', false)).toEqual([]);
    expect(toggleAction(['kick_root'], 'cancel_all', false)).toEqual(['kick_root']);
  });

  it('toggleAction 对可预授权动作正常增删', () => {
    expect(toggleAction([], 'kick_root', true)).toEqual(['kick_root']);
    expect(toggleAction(['kick_root', 'retry_node'], 'kick_root', true)).toEqual(['retry_node']);
  });

  it('selectableActions 只留下可预授权的那些', () => {
    const catalog = [
      item({ action: 'kick_root', pre_authorizable: true }),
      item({ action: 'cancel_all', risk: 'high', pre_authorizable: false }),
    ];
    expect(selectableActions(catalog).map((entry) => entry.action)).toEqual(['kick_root']);
  });

  it('RISK_ORDER 把高风险排在前面', () => {
    expect(RISK_ORDER.high).toBeLessThan(RISK_ORDER.medium);
    expect(RISK_ORDER.medium).toBeLessThan(RISK_ORDER.low);
  });
});

// ---------- Q2 标签 ----------

describe('Q2 三档标签必须解释清楚', () => {
  it('高风险明确写"永远需要你确认"', () => {
    expect(riskLabel(item({ risk: 'high', pre_authorizable: false }))).toContain('永远需要你确认');
  });

  it('中/低风险写明可预授权', () => {
    expect(riskLabel(item({ risk: 'medium' }))).toContain('可预授权');
    expect(riskLabel(item({ risk: 'low' }))).toContain('可预授权');
  });

  it('三档文案互不相同', () => {
    const labels = (['low', 'medium', 'high'] as const).map((risk) =>
      riskLabel(item({ risk, pre_authorizable: risk !== 'high' })),
    );
    expect(new Set(labels).size).toBe(3);
  });
});

// ---------- Q3 校验 ----------

describe('Q3 缺什么要说清什么', () => {
  it('没勾动作时指向动作', () => {
    const problem = validateDraft({ actions: [], minutes: '30', signature: 'me' });
    expect(problem.field).toBe('actions');
    expect(problem.message).toContain('勾');
  });

  it('期限非法时指向期限（含 0 与负数）', () => {
    for (const minutes of ['0', '-5', '', 'abc']) {
      const problem = validateDraft({ actions: ['kick_root'], minutes, signature: 'me' });
      expect(problem.field, `minutes=${minutes}`).toBe('minutes');
      expect(problem.message).toContain('期限');
    }
  });

  it('没署名时指向署名', () => {
    const problem = validateDraft({ actions: ['kick_root'], minutes: '30', signature: '   ' });
    expect(problem.field).toBe('signature');
    expect(problem.message).toContain('署名');
  });

  it('都齐了就给空前缀（界面据此放行）', () => {
    const problem = validateDraft({ actions: ['kick_root'], minutes: '30', signature: 'me' });
    expect(problem.field).toBeNull();
    expect(problem.message).toBe('');
  });

  it('校验顺序：先动作、再期限、最后署名（用户从前往后补）', () => {
    expect(validateDraft({ actions: [], minutes: '0', signature: '' }).field).toBe('actions');
    expect(validateDraft({ actions: ['a'], minutes: '0', signature: '' }).field).toBe('minutes');
  });
});

// ---------- Q4 剩余时间 ----------

describe('Q4 有效期与过期要分开', () => {
  it('小时级、分钟级、秒级各有可读文案', () => {
    expect(remainingText(grant({ remaining_seconds: 7200 }))).toContain('2 小时');
    expect(remainingText(grant({ remaining_seconds: 600 }))).toContain('10 分');
    expect(remainingText(grant({ remaining_seconds: 45 }))).toContain('45 秒');
  });

  it('过期时明确说"已过期"，不只显示 0', () => {
    expect(remainingText(grant({ remaining_seconds: 0 }))).toContain('已过期');
    expect(remainingText(grant({ remaining_seconds: -10 }))).toContain('已过期');
  });

  it('生效中要写出目标范围；不限目标时明说', () => {
    expect(grantWarning(grant({ targets: ['node-a'] }))).toContain('node-a');
    expect(grantWarning(grant({ targets: [] }))).toContain('不限目标');
  });

  it('生效中必须提醒"高风险不在此范围"', () => {
    expect(grantWarning(grant())).toContain('高风险');
  });

  it('已过期时给的是"需要你确认"而不是"生效中"', () => {
    const text = grantWarning(grant({ remaining_seconds: 0 }));
    expect(text).toContain('已过期');
    expect(text).not.toContain('生效中');
  });
});
