/**
 * 按需轮询的判据（P5-2）。
 *
 * 要钉的是 ROADMAP 那句**"空闲时不产生周期性请求"**。它最容易做成"看起来改了，
 * 其实还在拉"——所以判据是**数 tick 次数**，不是看代码里有没有 setInterval。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | P1 | 页面不可见时**一次都不拉** | 整晚开着的标签页仍然每 10 秒打一次 API |
 * | P2 | 切回可见时**立刻拉一次** | 用户切回来看到的是旧数据（还以为卡住了） |
 * | P3 | 重复 `start()` 不叠加定时器 | 每次可见性变化都多一个定时器 → 请求数翻倍增长 |
 * | P4 | 拿不到可见性信息时**当作可见** | 环境不支持就完全不刷新 |
 * | P5 | `stop()` 之后不再拉 | 卸载后仍在后台拉 |
 */
import { describe, expect, it } from 'vitest';

import { createPoller, type TimerApi, type VisibilityTarget } from './polling';

/** 手动驱动的定时器替身 —— 这样"拉了几次"是**可控且可数**的。 */
function fakeTimers() {
  const handlers = new Map<number, () => void>();
  let next = 1;
  const api: TimerApi = {
    setInterval: (handler) => {
      const handle = next++;
      handlers.set(handle, handler);
      return handle;
    },
    clearInterval: (handle) => {
      handlers.delete(handle);
    },
  };
  return {
    api,
    /** 跑一轮定时器回调。 */
    fire: () => {
      for (const handler of [...handlers.values()]) handler();
    },
    activeCount: () => handlers.size,
  };
}

/** 可见性替身。`set()` 会**同步**通知监听者（真实浏览器是异步的，但这里要可控）。 */
type FakeVisibility = VisibilityTarget & { set: (state: string) => void; listenerCount: () => number };

function fakeVisibility(initial: string): FakeVisibility {
  const listeners = new Set<() => void>();
  const target: FakeVisibility = {
    visibilityState: initial,
    addEventListener: (_type, listener) => listeners.add(listener),
    removeEventListener: (_type, listener) => listeners.delete(listener),
    set: (state: string) => {
      target.visibilityState = state;
      for (const listener of [...listeners]) listener();
    },
    listenerCount: () => listeners.size,
  };
  return target;
}

// ---------- P1 不可见不拉 ----------

describe('P1 页面不可见时一次都不拉', () => {
  it('不可见时 start 不产生 tick，也不装定时器', () => {
    const timers = fakeTimers();
    const visibility = fakeVisibility('hidden');
    let ticks = 0;
    const poller = createPoller({
      tick: () => {
        ticks += 1;
      },
      intervalMs: 10000,
      visibility: visibility,
      timers: timers.api,
    });

    poller.attachVisibility(visibility);
    expect(ticks).toBe(0);
    expect(poller.isRunning()).toBe(false);

    timers.fire();
    expect(ticks).toBe(0);
  });

  it('从可见切到不可见会**停掉**定时器', () => {
    const timers = fakeTimers();
    const visibility = fakeVisibility('visible');
    let ticks = 0;
    const poller = createPoller({
      tick: () => {
        ticks += 1;
      },
      intervalMs: 10000,
      visibility: visibility,
      timers: timers.api,
    });
    poller.attachVisibility(visibility);
    expect(poller.isRunning()).toBe(true);
    const afterStart = ticks;

    timers.fire();
    expect(ticks).toBe(afterStart + 1);

    visibility.set('hidden');
    expect(poller.isRunning()).toBe(false);
    timers.fire();
    expect(ticks).toBe(afterStart + 1); // 停掉之后一次都不多
  });
});

// ---------- P2 切回立刻拉 ----------

describe('P2 切回可见时立刻拉一次', () => {
  it('hidden → visible 会马上 tick，不用等一个间隔', () => {
    const timers = fakeTimers();
    const visibility = fakeVisibility('hidden');
    let ticks = 0;
    const poller = createPoller({
      tick: () => {
        ticks += 1;
      },
      intervalMs: 60000,
      visibility: visibility,
      timers: timers.api,
    });
    poller.attachVisibility(visibility);
    expect(ticks).toBe(0);

    visibility.set('visible');
    // **没有 fire 定时器** —— 立刻拉是这一步的全部意义。
    expect(ticks).toBe(1);
  });

  it('挂载时已经可见就直接开始（附着即对齐初始状态）', () => {
    const timers = fakeTimers();
    const visibility = fakeVisibility('visible');
    let ticks = 0;
    createPoller({
      tick: () => {
        ticks += 1;
      },
      intervalMs: 60000,
      visibility: visibility,
      timers: timers.api,
    }).attachVisibility(visibility);
    expect(ticks).toBe(1);
  });
});

// ---------- P3 幂等 ----------

describe('P3 重复 start 不叠加定时器', () => {
  it('连着 start 三次只装一个定时器，也只拉一次', () => {
    const timers = fakeTimers();
    const visibility = fakeVisibility('visible');
    let ticks = 0;
    const poller = createPoller({
      tick: () => {
        ticks += 1;
      },
      intervalMs: 10000,
      visibility: visibility,
      timers: timers.api,
    });

    poller.start();
    poller.start();
    poller.start();

    expect(timers.activeCount()).toBe(1);
    expect(ticks).toBe(1);

    timers.fire();
    expect(ticks).toBe(2); // 一轮只多一次，不是三次
  });

  it('可见性反复切换不会让定时器越积越多', () => {
    const timers = fakeTimers();
    const visibility = fakeVisibility('visible');
    const poller = createPoller({
      tick: () => undefined,
      intervalMs: 10000,
      visibility: visibility,
      timers: timers.api,
    });
    poller.attachVisibility(visibility);
    for (let index = 0; index < 5; index += 1) {
      visibility.set('hidden');
      visibility.set('visible');
    }
    expect(timers.activeCount()).toBe(1);
  });
});

// ---------- P4 拿不到可见性 ----------

describe('P4 拿不到可见性信息时当作可见', () => {
  it('没有 visibilityState 时照常轮询', () => {
    const timers = fakeTimers();
    let ticks = 0;
    const poller = createPoller({
      tick: () => {
        ticks += 1;
      },
      intervalMs: 10000,
      visibility: {},
      timers: timers.api,
    });
    poller.start();
    // 宁可多拉一次，也不要因为环境不支持而完全不刷新。
    expect(ticks).toBe(1);
    expect(poller.isRunning()).toBe(true);
  });

  it('visibility 为 null 时也不崩', () => {
    const timers = fakeTimers();
    const poller = createPoller({
      tick: () => undefined,
      intervalMs: 10000,
      visibility: null,
      timers: timers.api,
    });
    expect(() => poller.start()).not.toThrow();
  });
});

// ---------- P5 stop ----------

describe('P5 stop 之后不再拉', () => {
  it('stop 会清掉定时器', () => {
    const timers = fakeTimers();
    let ticks = 0;
    const poller = createPoller({
      tick: () => {
        ticks += 1;
      },
      intervalMs: 10000,
      visibility: { visibilityState: 'visible' },
      timers: timers.api,
    });
    poller.start();
    expect(ticks).toBe(1);

    poller.stop();
    expect(poller.isRunning()).toBe(false);
    expect(timers.activeCount()).toBe(0);
    timers.fire();
    expect(ticks).toBe(1);
  });

  it('stop 之后还能再 start（卸载/重挂不会把它废掉）', () => {
    const timers = fakeTimers();
    const poller = createPoller({
      tick: () => undefined,
      intervalMs: 10000,
      visibility: { visibilityState: 'visible' },
      timers: timers.api,
    });
    poller.start();
    poller.stop();
    poller.start();
    expect(poller.isRunning()).toBe(true);
  });

  it('detach 之后可见性变化不再触发', () => {
    const timers = fakeTimers();
    const visibility = fakeVisibility('hidden');
    let ticks = 0;
    const poller = createPoller({
      tick: () => {
        ticks += 1;
      },
      intervalMs: 10000,
      visibility: visibility,
      timers: timers.api,
    });
    const detach = poller.attachVisibility(visibility);
    detach();
    expect(visibility.listenerCount()).toBe(0);

    visibility.set('visible');
    expect(ticks).toBe(0);
  });
});
