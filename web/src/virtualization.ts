/**
 * 长列表虚拟化：给画布**有界**的渲染窗口（P5-2 最后一项）。
 *
 * ## 为什么不是"再装一个虚拟滚动库"
 *
 * 这个项目的画布是 `@xyflow/react`：它有自己的视口裁剪与变换，套一层第三方虚拟滚动
 * 只会和它的坐标系统打架。真正的代价也不在 DOM 数量，而在**在把节点交给 ReactFlow 之前
 * 的那一步**：`layoutBattlefield(...)`（elkjs）要对每一个节点算坐标。千节点时那一步是
 * 主线程上的大头，而 `ReactFlow` 的 `onlyRenderVisibleElements` 管不到它。
 *
 * 所以这里做的是**布局窗口**：在布局之前把要渲染的任务切成有界的一片。
 *
 * ## 三条不可动的规矩（都由 `virtualization.test.ts` 钉住）
 *
 * 1. **窗口外的东西要出声**：返回 `omitted`，界面上写"还有 N 个节点未渲染"。
 *    悄悄丢节点比慢更坏 —— 用户会以为树就这么多。
 * 2. **选中节点永远在窗口里**：否则点一下节点、它自己消失（本次会话实测过同类问题：
 *    筛选把选中节点藏起来时，详情面板还在显示它）。
 * 3. **祖先永远保留**：只留叶子会画出一堆没有父节点的方块，边的两端对不上。
 *
 * ## 独立性
 *
 * 这是**纯函数**：不碰 DOM、不碰 React、不看时钟。所以它能在 vitest 里直接测
 * （渲染层面的"首屏 < 2s"需要真浏览器，见 `docs/scale-baseline.md` 的边界说明）。
 */
import type { BattlefieldTask } from './types';

/** 一次最多交给布局器的节点数。 */
export const DEFAULT_WINDOW_LIMIT = 400;

/** 窗口里那个"要不要给 `layoutBattlefield` 的"子集 —— 见文件头第 1 条（不出声就是骗人）。 */
export interface TaskWindow {
  /** 交给布局器的任务（顺序与输入一致）。 */
  tasks: BattlefieldTask[];
  /** 被窗口挡掉的数量。**必须显示出来**（见文件头第 1 条）。 */
  omitted: number;
  /** 实际生效的窗口大小（可能因为"选中节点必须在内"而变大）。 */
  limit: number;
}

/**
 * 把 `tasks` 切成有界窗口。
 *
 * 取舍顺序：
 *   1. 选中节点先占位（它是交互焦点，必须可见）；
 *   2. 其余位置按"深度浅的优先" —— 树的上半部分提供结构，后半部分提供细节，
 *      只让看一半时留下上半部分的收益更大。
 *
 * `limit <= 0` 视为不限制（返回全部）：调用方用 0 表示"关掉虚拟化"，
 * 而"关掉"与"窗口大小为 0"是两件完全不同的事，容易写错，所以在类型上就分开。
 */
export function windowTasks(
  tasks: BattlefieldTask[],
  options: { limit?: number; selectedId?: string | null } = {},
): TaskWindow {
  const limit = options.limit ?? DEFAULT_WINDOW_LIMIT;
  if (limit <= 0 || tasks.length <= limit) {
    return { tasks: [...tasks], omitted: 0, limit: Math.max(limit, tasks.length) };
  }

  const keep = new Set<string>();
  const selectedId = options.selectedId ?? null;
  if (selectedId) {
    const index = tasks.findIndex((task) => task.id === selectedId);
    if (index >= 0) {
      // 选中节点**连同它的祖先链**先占位：只保自己会让它在树上"悬空"（边的一端没了），
      // 而用户此刻正盯着它。
      for (const id of ancestorChain(tasks, selectedId)) keep.add(id);
    }
  }

  // 深度浅的优先；同深度按原顺序（后端给的是树的顺序，别打乱它）。
  const ordered = tasks
    .map((task, index) => ({ task, index }))
    .sort((left, right) => left.task.depth - right.task.depth || left.index - right.index);

  for (const item of ordered) {
    if (keep.size >= limit) break;
    keep.add(item.task.id);
  }

  const windowed = tasks.filter((task) => keep.has(task.id));
  return {
    tasks: windowed,
    omitted: tasks.length - windowed.length,
    // 祖先链可能把窗口撑过 `limit`（必须撑，见上面那条注释）—— 所以 `limit` 记的是
    // **请求的**上限，真实数量看 `tasks.length`。两处都返回，免得读的人以为它们相等。
    limit,
  };
}

/** 界面文案里那句提示，也用在测试里。窗口没挡掉东西时返回空串（不显示提示）。 */
export function windowNotice(window: TaskWindow): string {
  if (window.omitted <= 0) return '';
  return `另有 ${window.omitted} 个节点被窗口挡住（缩小显示深度或折叠分支就能看到）。`;
}

/** 从 `id` 往上的祖先链（含自己）。数据不完整时返回能走到的那一段。 */
function ancestorChain(tasks: BattlefieldTask[], id: string): string[] {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const chain: string[] = [];
  const seen = new Set<string>();
  let cursor: string | null | undefined = id;
  while (cursor && !seen.has(cursor)) {
    seen.add(cursor);
    chain.push(cursor);
    cursor = byId.get(cursor)?.parent_id ?? null;
  }
  return chain;
}
