/**
 * 树数据的刷新通路（P5-2：从 `useBattlefieldController` 抽出来）。
 *
 * 三条规矩（都在下面的注释里，抽出来时一起搬过来了）：
 *   1. **SSE 是主通路**：树有变化就推事件，2 秒防抖后拉一次；
 *   2. **轮询是兜底**：SSE 静默断开时（代理掐连接、网络抖动）没它界面会一直显示旧数据；
 *   3. **页面不可见时完全不拉**：`createPoller` 只在可见时启动。
 */
import { useEffect } from 'react';

import { createPoller } from './polling';

export function useTreeRefresh({
  loadTree,
  onError,
}: {
  loadTree: () => Promise<void>;
  onError: (message: string) => void;
}) {
  useEffect(() => {
    // ⚠️ 这里**只拉一次**。原来写了两遍 `loadTree()`（相邻两行、同样的 catch），
    // 一次首屏因此打两个 `/api/tree` —— 千节点下服务端单次实测 0.2–2.3 秒、荷载 567KB，
    // 两个请求的 URL 逐字相同（`web/e2e/perf-first-screen.spec.ts` 的拆解
    // `tree_requests: 2` 就是它）。留一次就够：SSE 会推后续变化，`createPoller` 是兜底。
    //
    // 诚实补一句：删掉这一遍**没有让首屏变快**（2422–4810ms → 2985–4289ms，同一量级）。
    // 真正的瓶颈是客户端处理那份 567KB 荷载：同一次请求浏览器侧量到 2178ms，
    // 而 Node 侧只要 230ms —— 差在主线程。别把"少一个请求"读成"首屏达标"。
    loadTree().catch((err: unknown) => onError(String(err)));

    // 事件流是**主刷新通路**（P5-2）：树有变化时后端推事件，这里 2 秒后拉一次。
    // 保留这个防抖是因为一次状态转换往往连着好几条事件，逐条拉会打成一片。
    const source = new EventSource('/api/events');
    let eventRefreshTimer: number | null = null;
    source.onmessage = () => {
      if (eventRefreshTimer !== null) {
        window.clearTimeout(eventRefreshTimer);
      }
      eventRefreshTimer = window.setTimeout(() => {
        eventRefreshTimer = null;
        loadTree().catch(() => undefined);
      }, 2000);
    };

    // 轮询是**兜底**，不是主力：SSE 静默断开时（代理掐连接、网络抖动）没有它
    // 界面会一直显示旧数据而没人知道。所以留着，但把间隔从 10 秒放到 60 秒 ——
    // 而那 10 秒一次的开销才是真的浪费（`/api/tree` 在千节点下要 130ms）。
    //
    // 并且**页面不可见时完全不拉**（`createPoller`）：原来整晚开着没人看的标签页
    // 仍然每 10 秒打一次 API。切回可见时立刻拉一次，所以看不到旧数据。
    const poller = createPoller({
      tick: () => loadTree().catch(() => undefined),
      intervalMs: 60000,
    });
    const detachVisibility = poller.attachVisibility(
      typeof document === 'undefined' ? {} : document,
    );

    return () => {
      detachVisibility();
      poller.stop();
      if (eventRefreshTimer !== null) {
        window.clearTimeout(eventRefreshTimer);
      }
      source.close();
    };
  }, [loadTree, onError]);
}
