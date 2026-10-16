/**
 * 长列表虚拟化的判据（P5-2 最后一项）。
 *
 * ## 这一组在测什么、不在测什么
 *
 * **在测**：窗口的**契约** —— 有界、会出声、选中节点与它的祖先链一定在内、
 * 顺序不打乱、"关掉虚拟化"与"窗口为 0"是两件事。
 *
 * **不在测**：真浏览器里的"首屏 < 2s"。那需要真浏览器 + 千节点，属于
 * `docs/scale-baseline.md` 里明确写下的边界（那里量的是后端）。
 * 这里不假装测了它。
 */
import { describe, expect, it } from 'vitest';

import type { BattlefieldTask } from './types';
import { DEFAULT_WINDOW_LIMIT, windowNotice, windowTasks } from './virtualization';

/** 造一棵 `count` 个节点的链式树（每个节点的父是上一个）。 */
function chain(count: number): BattlefieldTask[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `n${index}`,
    parent_id: index === 0 ? null : `n${index - 1}`,
    depth: index,
    title: `节点 ${index}`,
    status: 'queued',
  })) as unknown as BattlefieldTask[];
}

/** 造一棵宽而浅的树：根 + `width` 个子节点（都 depth=1）。 */
function fanout(width: number): BattlefieldTask[] {
  // 显式标注元素类型：第一项的 `parent_id: null` 会让 TS 把数组推成
  // `parent_id: null` 的窄类型，后面塞 'root' 就报 TS2322（实测）。
  const tasks: Array<Record<string, unknown>> = [
    { id: 'root', parent_id: null, depth: 0, title: 'root', status: 'queued' },
  ];
  for (let index = 0; index < width; index += 1) {
    tasks.push({ id: `c${index}`, parent_id: 'root', depth: 1, title: `子 ${index}`, status: 'queued' });
  }
  return tasks as unknown as BattlefieldTask[];
}

// ---------- V1 有界 ----------

describe('V1 窗口是有界的', () => {
  it('节点数不超过上限', () => {
    const result = windowTasks(chain(1000), { limit: 50 });
    expect(result.tasks.length).toBeLessThanOrEqual(50);
    expect(result.limit).toBe(50);
  });

  it('默认上限是个常量，不是"看情况"', () => {
    const result = windowTasks(fanout(DEFAULT_WINDOW_LIMIT * 3));
    expect(result.tasks.length).toBeLessThanOrEqual(DEFAULT_WINDOW_LIMIT);
    expect(result.omitted).toBeGreaterThan(0);
  });

  it('没超过上限时原样返回、也不算"挡掉了东西"', () => {
    const tasks = fanout(10);
    const result = windowTasks(tasks, { limit: 50 });
    expect(result.tasks).toHaveLength(tasks.length);
    expect(result.omitted).toBe(0);
    expect(windowNotice(result)).toBe('');
  });
});

// ---------- V2 会出声 ----------

describe('V2 窗口外的东西必须被说出来', () => {
  it('omitted 与"留下的数量"加起来是总数', () => {
    const tasks = chain(1000);
    const result = windowTasks(tasks, { limit: 123 });
    expect(result.tasks.length + result.omitted).toBe(tasks.length);
  });

  it('被挡住时给出的提示里带**被挡住的数量**', () => {
    const result = windowTasks(chain(1000), { limit: 10 });
    const notice = windowNotice(result);
    expect(result.omitted).toBe(990);
    expect(notice).toContain('990');
    // 提示只说"挡了多少"；"渲染了多少"由界面把 `renderedTaskCount` 拼在前面
    // （两处分工写在 `LeftRail` 的注释里，避免提示里出现两遍同一个数字）。
    expect(notice).toContain('挡住');
  });
});

// ---------- V3 选中节点与祖先链 ----------

describe('V3 选中节点永远在窗口里，且不悬空', () => {
  it('选中的深层节点连同它的祖先链一起留下', () => {
    const tasks = chain(500);
    const selected = 'n400';
    const result = windowTasks(tasks, { limit: 20, selectedId: selected });
    const ids = new Set(result.tasks.map((task) => task.id));

    expect(ids.has(selected)).toBe(true);
    // 祖先链：n400 → n399 → … 一直到 root。链有多长，窗口就有多大（允许超出 limit）。
    expect(ids.has('n0')).toBe(true);
    expect(ids.has('n399')).toBe(true);
    expect(result.tasks.length).toBeGreaterThanOrEqual(401);
  });

  it('选中的是根时不会把整棵树都拉进来', () => {
    const result = windowTasks(chain(1000), { limit: 30, selectedId: 'n0' });
    expect(result.tasks.length).toBeLessThanOrEqual(30);
    expect(new Set(result.tasks.map((task) => task.id)).has('n0')).toBe(true);
  });

  it('选了一个不在当前视图里的 id 时不影响窗口', () => {
    const result = windowTasks(chain(100), { limit: 10, selectedId: '不存在' });
    expect(result.tasks).toHaveLength(10);
  });
});

// ---------- V4 顺序与内容 ----------

describe('V4 不打乱原顺序、优先留浅的', () => {
  it('留下的节点保持输入顺序（后端给的是树的顺序）', () => {
    const tasks = fanout(100);
    const result = windowTasks(tasks, { limit: 20 });
    const original = tasks.map((task) => task.id);
    const kept = result.tasks.map((task) => task.id);
    expect(kept).toEqual(original.filter((id) => kept.includes(id)));
  });

  it('宽而浅时优先留深度小的（结构优先于细节）', () => {
    const tasks = [
      { id: 'deep', parent_id: null, depth: 5, title: 'deep', status: 'queued' },
      { id: 'shallow', parent_id: null, depth: 0, title: 'shallow', status: 'queued' },
    ] as unknown as BattlefieldTask[];
    const result = windowTasks(tasks, { limit: 1 });
    expect(result.tasks.map((task) => task.id)).toEqual(['shallow']);
  });
});

// ---------- V5 "关掉"与"窗口为 0"不是一回事 ----------

describe('V5 limit 的语义', () => {
  it('limit = 0 表示关掉虚拟化（全部返回），不是"一个都不要"', () => {
    const tasks = chain(500);
    const result = windowTasks(tasks, { limit: 0 });
    expect(result.tasks).toHaveLength(tasks.length);
    expect(result.omitted).toBe(0);
  });

  it('负数同理（只可能来自配置写错，按"关掉"处理而不是塌成空画布）', () => {
    const tasks = chain(10);
    expect(windowTasks(tasks, { limit: -1 }).tasks).toHaveLength(tasks.length);
  });
});

// ---------- V6 空输入 ----------

describe('V6 空输入不炸', () => {
  it('空树返回空窗口', () => {
    const result = windowTasks([]);
    expect(result.tasks).toEqual([]);
    expect(result.omitted).toBe(0);
  });
});
