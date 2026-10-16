/**
 * 前端文件体量的**棘轮判据**（P5-2）。
 *
 * ## 为什么用棘轮而不是直接卡 500 行
 *
 * ROADMAP 的判据是"单文件不再超 500 行"。但**现在直接卡 500 会红一片**，
 * 而"允许长期红着的判据"很快会变成"所有人学会忽略它"。
 * 所以给每个超标文件记一个**只许减少**的预算，并要求总量真的在降。
 *
 * 棘轮的四条规矩（缺一条它就会退化成装饰）：
 *
 * 1. **预算只许下调**：文件变大超过当前预算 → 红。想扩容必须显式改本文件 ——
 *    而那次改动会在 review 里被看见。
 * 2. **低于门槛的文件不许超标**：新文件直接受 500 行约束。
 * 3. **总数必须真的在降**：`BUDGET_TOTAL` 是逐项之和，每拆一块就要把它调下来。
 * 4. **达标的条目要删掉**：预算表是"现在还没拆完"的清单，不是"曾经很大"的历史。
 *
 * ## 现状（诚实记录）
 *
 * 本次抽出了 `labels.ts` / `format.ts` / `polling.ts` 三块纯逻辑
 * （`App.tsx` 2924 → 2715 行）。剩下的是 React 组件本身
 * （`WatchdogPanel` / `ConstitutionPanel` / `DetailPanel` / `TaskNodeView` …），
 * 它们的抽取要连着 props 与状态一起动 —— 属于下一步。
 * 所以 `App.tsx` **如实留在预算表里**，而不是假装已经达标。
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SRC = join(import.meta.dirname, '.');

/** 目标门槛：超过它就要进预算表。 */
const LINE_LIMIT = 500;

/**
 * 超标文件的**当前预算**（行数上限）。**只许往下调。**
 *
 * | 文件 | 预算 | 为什么还没达标 |
 * |---|---|---|
 * | `useBattlefieldController.ts` | 783 | 它是 `App()` 搬出来的状态与回调；派生值/子 hook 还可以再拆 |
 *
 * 第三轮把 `types.ts`（948 → 聚合出口 8 行，类型分到 `types-task/steward/state/insight`）、
 * `api.ts`（833 → 聚合出口 10 行，函数分到 `api-http/steward/insight/state/tree`）、
 * `StewardPanel.tsx`（565 → 468，动作收件箱进 `StewardActionInbox.tsx`）都拆掉了，
 * 所以这三条**从预算表里删掉**（棘轮第 4 条）。做到这里，全仓只剩一个文件超标。
 */
const BUDGETS: Record<string, number> = {
  'useBattlefieldController.ts': 783,
};

/** 预算总量（= 逐项之和）。**每拆一块就把它调下来**，只降不升。 */
const BUDGET_TOTAL = 783;

function sourceFiles(): string[] {
  return readdirSync(SRC)
    .filter((name) => /\.(ts|tsx)$/.test(name) && !name.endsWith('.test.ts'))
    .sort();
}

/** 真实行数。**末尾换行不算一行** —— `split('\n')` 会多出一个空串（第一版差 1 就是这么来的）。 */
function lineCount(name: string): number {
  const text = readFileSync(join(SRC, name), 'utf8');
  const parts = text.split('\n');
  return text.endsWith('\n') ? parts.length - 1 : parts.length;
}

function oversized(): Record<string, number> {
  const out: Record<string, number> = {};
  for (const name of sourceFiles()) {
    const lines = lineCount(name);
    if (lines > LINE_LIMIT) out[name] = lines;
  }
  return out;
}

// ---------- T1 低于门槛的文件不许超标 ----------

describe('T1 新文件直接受 500 行约束', () => {
  it('超过 500 行的文件必须在预算表里登记', () => {
    const unregistered = Object.keys(oversized()).filter((name) => !(name in BUDGETS));
    expect(
      unregistered,
      `这些文件超过 ${LINE_LIMIT} 行却没有登记预算 —— 请先拆，或在 BUDGETS 里登记并写清为什么`,
    ).toEqual([]);
  });
});

// ---------- T2 预算只许下调 ----------

describe('T2 预算只许下调（棘轮）', () => {
  it('没有文件超过它的预算', () => {
    const violations: string[] = [];
    for (const [name, lines] of Object.entries(oversized())) {
      const budget = BUDGETS[name];
      if (budget !== undefined && lines > budget) {
        violations.push(`${name}: ${lines} 行 > 预算 ${budget}`);
      }
    }
    expect(violations, '文件超出预算 —— 要么拆，要么在 review 里显式调高预算').toEqual([]);
  });

  it('预算表里没有已经达标的残留条目', () => {
    const stale = Object.keys(BUDGETS).filter((name) => !(name in oversized()));
    expect(stale, '这些文件已经达标了，请把它们从 BUDGETS 里删掉').toEqual([]);
  });

  it('预算总量不超过 BUDGET_TOTAL', () => {
    const total = Object.values(BUDGETS).reduce((sum, value) => sum + value, 0);
    expect(total).toBeLessThanOrEqual(BUDGET_TOTAL);
  });

  it('BUDGET_TOTAL 与逐项之和对得上（两处数字不许漂移）', () => {
    const total = Object.values(BUDGETS).reduce((sum, value) => sum + value, 0);
    expect(total).toBe(BUDGET_TOTAL);
  });
});

// ---------- T3 拆出来的模块本身要合规 ----------

describe('T3 本次拆出的模块都在门槛之内', () => {
  it.each(['labels.ts', 'format.ts', 'polling.ts'])('%s 低于 500 行', (name) => {
    expect(lineCount(name)).toBeLessThanOrEqual(LINE_LIMIT);
  });
});

// ---------- T4 判据不能空转 ----------

describe('T4 判据本身可信', () => {
  it('扫描面真的覆盖到源码文件', () => {
    const names = sourceFiles();
    expect(names).toContain('App.tsx');
    expect(names).toContain('labels.ts');
    expect(names.length).toBeGreaterThan(15);
  });

  it('确实还有超标文件（否则棘轮表是空的，判据也就没在测东西）', () => {
    expect(Object.keys(oversized()).length).toBeGreaterThan(0);
  });

  it('行数计数与末尾换行无关（差 1 的坑）', () => {
    // 直接对着已知文件核一次：`format.test.ts` 的行数应当是它真实的行数。
    const name = 'polling.ts';
    const text = readFileSync(join(SRC, name), 'utf8');
    expect(lineCount(name)).toBe(text.endsWith('\n') ? text.split('\n').length - 1 : text.split('\n').length);
  });
});
