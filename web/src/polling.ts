/**
 * **按需轮询**（P5-2）：页面不可见时不产生周期性请求。
 *
 * ## 要解决的问题
 *
 * 原来的写法是裸的 `setInterval(loadTree, 10000)` —— 于是**页面被切到后台、
 * 甚至整晚开着没人看的时候，它仍然每 10 秒打一次 API**。这不是"有点浪费"：
 * 它会让 `/api/tree` 这种要扫库的端点在无人看护时持续占资源，
 * 而 `test_scale.py` 量到的正是这个端点在千节点下的耗时。
 *
 * ## 做法：可见性驱动的启停
 *
 * * 页面可见 → 按间隔轮询，并且**立刻拉一次**（切回来时用户要立刻看到新的）；
 * * 页面不可见 → **停掉定时器**（不是"继续跑但跳过"——那还是会有一次判断开销，
 *   而且代码更长）。
 *
 * ## 为什么不直接换成 SSE
 *
 * 后端确实有 `/api/steward/alerts/stream`，但整棵树的快照**不适合走 SSE**：
 * 一棵千节点的树的荷载是 640KB（`docs/scale-baseline.md`），推流的性价比很低，
 * 而"用户在看的时候才拉"本来就是这个界面的真实使用形态。
 * 真正省掉请求的是**可见性**，不是传输方式 —— 所以先做这一层。
 *
 * ## 一个必须说清的取舍
 *
 * 不可见时**完全不拉**意味着：后台开着一整晚，回来看到的是最后一次拉到的数据。
 * 但那没关系 —— 切回可见时我们立刻拉一次，中间那段"在后台看旧数据"没有意义。
 */

export interface Poller {
  /** 立刻拉一次并启动定时器（幂等：重复调用不会叠加定时器）。 */
  start: () => void;
  /** 停掉定时器（不影响已经发出的那一次请求）。 */
  stop: () => void;
  /** 当前是否在轮询。 */
  isRunning: () => boolean;
  /** 注册可见性变化监听；返回取消函数。 */
  attachVisibility: (target: VisibilityTarget) => () => void;
}

/** 只用到 `visibilityState` 与 `addEventListener`/`removeEventListener` —— 便于测试替身。 */
export interface VisibilityTarget {
  visibilityState?: string;
  addEventListener?: (type: string, listener: () => void) => void;
  removeEventListener?: (type: string, listener: () => void) => void;
}

/** 定时器的最小接口（测试里换成手动触发）。 */
export interface TimerApi {
  setInterval: (handler: () => void, ms: number) => number;
  clearInterval: (handle: number) => void;
}

export function createPoller(options: {
  /** 每次到点要做的事。 */
  tick: () => void;
  /** 间隔（毫秒）。 */
  intervalMs: number;
  /** 可见性来源；缺省用 `document`。 */
  visibility?: VisibilityTarget | null;
  /** 定时器实现；缺省用 `window`。 */
  timers?: TimerApi | null;
}): Poller {
  const timers: TimerApi =
    options.timers ??
    ({
      setInterval: (handler, ms) => window.setInterval(handler, ms) as unknown as number,
      clearInterval: (handle) => window.clearInterval(handle),
    } satisfies TimerApi);
  const visibility = options.visibility ?? (typeof document === 'undefined' ? null : document);

  let handle: number | null = null;

  const visible = (): boolean => {
    // 拿不到可见性时**当作可见**：宁可多拉一次，也不要因为环境不支持而完全不刷新。
    const state = visibility?.visibilityState;
    return state === undefined ? true : state === 'visible';
  };

  const stop = (): void => {
    if (handle !== null) {
      timers.clearInterval(handle);
      handle = null;
    }
  };

  const start = (): void => {
    if (handle !== null) return; // 幂等：重复 start 不叠加
    if (!visible()) return; // 不可见就不启动
    options.tick(); // 先拉一次 —— 切回来时用户要立刻看到新的
    handle = timers.setInterval(options.tick, options.intervalMs);
  };

  const attachVisibility = (target: VisibilityTarget): (() => void) => {
    const onChange = (): void => {
      if (visible()) {
        start();
      } else {
        stop();
      }
    };
    target.addEventListener?.('visibilitychange', onChange);
    // 初始状态对齐一次：挂载时页面可能已经是不可见的（后台标签页里打开）。
    onChange();
    return () => target.removeEventListener?.('visibilitychange', onChange);
  };

  return { start, stop, isRunning: () => handle !== null, attachVisibility };
}
