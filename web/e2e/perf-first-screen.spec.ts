/**
 * P3-6 / P5-2：**千节点下界面首屏 < 2s**（真浏览器 + 真后端 + 真产物）。
 *
 * ## 这一条量的是什么
 *
 * 从 `page.goto` 发出，到 L4 画布上**出现第一个节点**为止 —— 也就是"用户打开界面、
 * 看到树"的时间。它包含三件事：取到 index.html 与 bundle、拿到
 * `/api/tree?root_id=…` 的响应、首帧把节点画出来。
 *
 * ## 三个刻意的选择
 *
 * 1. **走 API 托管的 `web/dist`（生产产物），不走 Vite dev server。**
 *    容器与部署就是这条路径；dev server serve 的是未打包模块，数字会差一个量级，
 *    拿它当"首屏"等于给自己发一张假的合格证。
 * 2. **造树用 `seed_scale_tree.py`**，与后端规模判据（`api/tests/test_scale.py`）**同形**
 *    （根 + 4 叉 + 叶子带运行记录）。两棵树形状不同的话，"后端 130ms"与"前端 < 2s"
 *    就不是同一件事的两个数字。
 * 3. **数字必须落到文件里**（`web/.e2e/perf-first-screen.json`）：只记"通过"的判据
 *    会在某天悄悄爬到 1.9 秒而没人知道；而落盘的数字能被文档引用、能被下一次对比。
 *
 * ## 诚实的边界（写在最前面，免得被读成"首屏体验全测了"）
 *
 * * **不含浏览器进程启动**（Playwright 已经起好了浏览器）—— 真实用户冷启动会多几百毫秒；
 * * 只在这**一台机器**上量过（`docs/scale-baseline.md` §5 记了机型与数字）；
 * * 不含首屏之后的交互（虚拟化是否把 1000 个节点都交给布局器、拖拽是否跟手）——
 *   那一条由 `virtualization.test.ts` 与 l4-flow 的"树小时不出声"分别管。
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { expect, test } from '@playwright/test';

import { API_PORT, DB_URL, STATE_DIR, WEB_ROOT, backendPython } from './e2e-state';

const NODES = 1000;
const ROOT_ID = 'perf-root-1000';
/**
 * ROADMAP 的目标值（P3-6/P5-2：**界面可交互，首屏 < 2s**）。
 *
 * ⚠️ 实测**没有达到**：本机冷启 3.2–4.8 秒（数字见 `web/.e2e/perf-first-screen.json`
 * 与 `docs/scale-baseline.md` §5）。这个常量留着是**故意的** —— 目标必须留在代码里，
 * 否则它会慢慢从"没做到"漂移成"没这回事"。
 */
const TARGET_MS = 2000;
/**
 * 棘轮：现在**只保证不比这更慢**（实测 4.8 秒，留约 1.3 倍余量）。
 *
 * 为什么不用 TARGET_MS 直接当断言：那会让这套 E2E 一直红，而"一直红"的门禁等于没有门禁
 * （见 `docs/test-determinism.md`）。棘轮与目标是两件事，**棘轮过了不代表目标达成** ——
 * 报告里也不许那么写。
 */
const RATCHET_MS = 6000;
const ARTIFACT = resolve(WEB_ROOT, '.e2e', 'perf-first-screen.json');

/**
 * 采样 400ms 内的**整机** CPU 占用（%）。
 *
 * 为什么需要它：这台开发机上常年跑着别人的任务，`/api/tree` 的 p50 会在 300ms 与 700ms
 * 之间摆。**量机器的时候不要假装在量产品** —— 超过阈值就 skip 并说明（见用例里的注释）。
 * 做法是两次 `os.cpus()` 的累计时间差（`process.cpuUsage()` 只能看到本进程，会恒为 0）。
 */
async function measureCpuBusyPercent(): Promise<number> {
  const os = await import('node:os');
  const snapshot = () => os.cpus().map((cpu) => ({ ...cpu.times }));
  const before = snapshot();
  await new Promise((resolveWait) => setTimeout(resolveWait, 400));
  const after = snapshot();
  let idle = 0;
  let total = 0;
  for (let index = 0; index < before.length; index += 1) {
    const b = before[index];
    const a = after[index];
    const deltaIdle = a.idle - b.idle;
    total += a.user - b.user + (a.nice - b.nice) + (a.sys - b.sys) + deltaIdle + (a.irq - b.irq);
    idle += deltaIdle;
  }
  return total > 0 ? ((total - idle) / total) * 100 : 0;
}


test.beforeAll(() => {
  const dist = resolve(WEB_ROOT, 'dist', 'index.html');
  // 不静默跳过：dist 不存在说明**这条判据这次没验**，必须让人看见（先跑 pnpm build）。
  expect(
    existsSync(dist),
    `前端产物不存在：${dist} —— 先跑 \`pnpm build\`（verify_all 的顺序本来就是 build 在前）`,
  ).toBe(true);

  const output = execFileSync(
    backendPython(),
    [
      resolve(WEB_ROOT, 'e2e', 'seed_scale_tree.py'),
      '--state-home',
      STATE_DIR,
      '--database-url',
      DB_URL,
      '--nodes',
      String(NODES),
      '--root-id',
      ROOT_ID,
    ],
    { cwd: WEB_ROOT, encoding: 'utf-8' },
  );
  const summary = JSON.parse(output.trim().split('\n').pop() ?? '{}') as { nodes?: number };
  expect(summary.nodes, `造树失败（应当是 ${NODES} 个节点）：${output}`).toBeGreaterThanOrEqual(NODES);
});

test(`千节点首屏（目标 ${TARGET_MS}ms，棘轮 ${RATCHET_MS}ms）`, async ({ page }) => {
  // CI 上**不跑**这条，并且把原因写出来（不是静默跳过）。
  //
  // 实测（run 38965425892，commit 3024253，windows-latest）：跑到这条时整套用例超时
  // （`apiRequestContext.get: Test timeout of 180000ms exceeded`，E2E 步 1 failed / 6 passed）。
  // 也就是说：**在这台 2 vCPU 的 CI runner 上，千节点首屏根本走不完 180 秒** ——
  // 那量出来的是 runner 的容量，不是"首屏"。CI 上的门禁因此不能背这条判据。
  //
  // 这条数字仍然有意义：说明"首屏 < 2s"在慢机器上差得更远（本机 2.4–4.8s）。
  // 要复现/专项跑：`cd web && pnpm build && pnpm exec playwright test e2e/perf-first-screen.spec.ts`
  test.skip(
    Boolean(process.env.CI),
    'CI runner 上千节点首屏 >180s（超过用例超时）：量的是 runner 容量而不是首屏，故只在非 CI 环境跑',
  );

  // **机器被别的活占满时不量**（2026-10-18 实测的假红）：
  // 这台开发机上同时跑着别人的 Houdini 队列、多个 python 服务、Codex 桌面进程，
  // 于是同一份代码的 `/api/tree` p50 会在 **300ms 与 700ms** 之间摆 ——
  // 一次全绿的 698ms（门槛 650）与一次 1440ms 的 TTFB（门槛 800）都是这么来的。
  // 那种情况下量到的是机器，不是产品；而有意义的做法是**跳过并说明**，
  // 不是把门槛调大（调大之后"扫描又回到请求路径"这种真回归就再也测不出来了）。
  const busyPercent = await measureCpuBusyPercent();
  test.skip(
    busyPercent > 45,
    `本机 CPU 已被别的进程占到 ${busyPercent.toFixed(0)}%（>45%）：此时的首屏/TTFB 量的是机器负载而不是产品`,
  );

  const url = `http://127.0.0.1:${API_PORT}/?root_id=${ROOT_ID}`;

  // 线索③（"渲染 1000 个节点要多久"）以前只能靠猜，因为 `tree_ms` 里混着"等网络"。
  // 用浏览器的 **long task** 观测来分离：主线程上一次超过 50ms 的阻塞都会被记下来，
  // 它才是"前端自己花的钱"。**不改产品代码**（`addInitScript` 只在这个测试的页面里生效）。
  await page.addInitScript(() => {
    const bucket: { start: number; duration: number }[] = [];
    (window as unknown as { __longTasks: typeof bucket }).__longTasks = bucket;
    try {
      const observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          bucket.push({ start: Math.round(entry.startTime), duration: Math.round(entry.duration) });
        }
      });
      observer.observe({ entryTypes: ['longtask'] });
    } catch {
      // 浏览器不支持 longtask 就**如实为空**（不伪造 0 —— 空数组与"没有长任务"是两件事，
      // 由 `long_tasks.supported` 区分）。
      bucket.push({ start: -1, duration: -1 });
    }
  });

  const started = Date.now();
  await page.goto(url);
  await expect(page.getByLabel('L4 自动导航画布').locator('.react-flow__node').first()).toBeVisible({
    timeout: 60_000,
  });
  const coldMs = Date.now() - started;

  // 拆解：把 4 秒拆成"装载 / 取树 / 渲染"，否则下一次只能靠猜。
  const breakdown = await page.evaluate(() => {
    const nav = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined;
    const resources = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
    const tree = resources.filter((item) => item.name.includes('/api/tree'));
    const scripts = resources.filter((item) => item.initiatorType === 'script');
    const sum = (items: PerformanceResourceTiming[]) => items.reduce((total, item) => total + item.duration, 0);
    return {
      load_ms: nav ? Math.round(nav.loadEventEnd) : null,
      dom_content_loaded_ms: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
      tree_requests: tree.length,
      tree_ms: Math.round(sum(tree)),
      tree_last_ms: tree.length ? Math.round(tree[tree.length - 1].duration) : null,
      // URL 必须记下来：只记"两次请求"会让人以为重复，而它们可能是两个不同的端点
      // （树 + 轮廓）—— 实测差点据此改错地方。
      //
      // `start_ms` / `wait_ms` 是这一轮新加的：它回答"请求是**何时发出**的、以及它在
      // 自己身上花了多久"。若 `start_ms` 很大而请求本身只有几百毫秒，那 1.5 秒里的大头
      // 就不是网络也不是服务端，而是**应用在拿到响应之前自己排的队**（可修的前端问题）；
      // 反过来若 `start_ms` 很小而 duration 很大，那才该去查服务端/竞争。
      tree_urls: tree.map((item) => {
        const startMs = Math.round(item.startTime);
        return {
          url: item.name.replace(/^https?:\/\/[^/]+/, ''),
          start_ms: startMs,
          wait_ms: Math.round(item.responseStart - item.requestStart),
          body_ms: Math.round(item.responseEnd - item.responseStart),
          duration_ms: Math.round(item.duration),
        };
      }),
      script_requests: scripts.length,
      script_ms: Math.round(sum(scripts)),
      // 首屏那一瞬间页面打了多少 API、以及**树的请求落在哪一段**。
      // 目的：把"handler 慢"和"被自己的并发压住"分开 —— 若树请求与十几个 API 同时飞，
      // 那 0.8–2.3 秒更像饱和排队，而不是这个 handler 本身贵。
      api_requests: resources
        .filter((item) => item.name.includes('/api/'))
        .map((item) => ({
          url: item.name.replace(/^https?:\/\/[^/]+/, '').split('?')[0],
          start_ms: Math.round(item.startTime),
          duration_ms: Math.round(item.duration),
        })),
    };
  });

  // 第二次数（热）：说明这个数字里有多少是"第一次必然要付的钱"。判据用冷的那个。
  const warmStarted = Date.now();
  await page.reload();
  await expect(page.getByLabel('L4 自动导航画布').locator('.react-flow__node').first()).toBeVisible({
    timeout: 60_000,
  });
  const warmMs = Date.now() - warmStarted;

  // 两次页面加载**各自**那次取树的 TTFB。判据取两者的**较小值**：
  // 后台扫描偶尔会与请求抢 GIL（实测孤点 ~1.1 秒），单次值会变成假失败；
  // 而"修好了"时至少有一次是 ~250ms、"坏掉了"时两次都是 ~1.0–1.2 秒（见下面的阈值讨论）。
  const warmTreeWait = await page.evaluate(() => {
    const entries = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
    const trees = entries.filter((item) => item.name.includes('/api/tree'));
    const last = trees[trees.length - 1];
    return last ? Math.round(last.responseStart - last.requestStart) : Number.NaN;
  });
  const coldTreeWait = Number(
    (breakdown.tree_urls[0] as { wait_ms?: number } | undefined)?.wait_ms ?? Number.NaN,
  );
  const bestTreeWait = Math.min(coldTreeWait, Number(warmTreeWait));

  const tree = await page.request.get(`http://127.0.0.1:${API_PORT}/api/tree?root_id=${ROOT_ID}`);
  const payloadBytes = (await tree.body()).length;

  // 拆解的第二段：**服务端**这一趟到底多贵？
  // 浏览器那边的 resource duration 会把"排队/等 CPU"也算进去，所以必须单独量一次请求本身，
  // 否则会把"服务端慢"误读成"前端慢"（或反过来）。
  const samples: number[] = [];
  for (let index = 0; index < 3; index += 1) {
    const tick = Date.now();
    const response = await page.request.get(`http://127.0.0.1:${API_PORT}/api/tree?root_id=${ROOT_ID}`);
    samples.push(Date.now() - tick);
    expect(response.ok()).toBe(true);
  }
  const serverMs = Math.min(...samples);

  // 服务端**自己的账**（P5-1 的 `/api/metrics`，按路由模板记滑动窗口分位数）。
  // 它和浏览器的 `wait_ms` 一比就能把两件事分开（这是本轮要问的问题）：
  //   * 两者接近            → 时间真的花在 **handler / 库** 里；
  //   * 服务端说很快而浏览器等很久 → 请求在**排队**（事件循环被同步工作占着 / 连接被占满）。
  const metricsResponse = await page.request.get(`http://127.0.0.1:${API_PORT}/api/metrics?limit=10`);
  const metricsPayload = metricsResponse.ok()
    ? ((await metricsResponse.json()) as { routes?: Record<string, unknown>[] } | Record<string, unknown>)
    : null;
  const metricsRoutes = (() => {
    if (!metricsPayload) return [] as Record<string, unknown>[];
    const payload = metricsPayload as { routes?: Record<string, unknown>[]; slowest?: Record<string, unknown>[] };
    return payload.routes ?? payload.slowest ?? [];
  })();
  const treeMetric = metricsRoutes.find((item) => String(item.key ?? '').includes('/api/tree')) ?? null;

  // DOM 规模：画布上真的放了多少个节点/边？
  // `virtualization.ts` 限的是"交给布局器的节点"（400），**不等于**渲染出来的 DOM 也少；
  // 这两个数字一比就能看出渲染那一秒花在哪。
  const domNodes = await page.locator('.react-flow__node').count();
  const domEdges = await page.locator('.react-flow__edge').count();

  // 静下来之后再量一次服务端：把"服务端本来就慢"和"被首屏并发压慢"分开。
  // （上面的 samples 是紧接着首屏取的，那时侧栏那批请求还在飞。）
  await page.waitForTimeout(2500);
  const quietSamples: number[] = [];
  for (let index = 0; index < 3; index += 1) {
    const tick = Date.now();
    const response = await page.request.get(`http://127.0.0.1:${API_PORT}/api/tree?root_id=${ROOT_ID}`);
    quietSamples.push(Date.now() - tick);
    expect(response.ok()).toBe(true);
  }

  // **更久之后**再量一次（这一批是上一轮加的对照组，用来分开两个解释）：
  //   1) "被侧栏那批请求挤慢" —— 那 2.5 秒的 `quietSamples` 就够了；
  //   2) "进程扫描缓存（TTL 2 秒）过期" —— 只有**彻底安静**（这一批）才会命中，
  //      因为 2.5 秒那批自己也把缓存刷热了一部分。
  // 两批一起看才分得清：若 very_quiet 的第一次仍然慢，那就是缓存/扫描；若它快了，
  // 那 quiet 那批的慢就只是并发。
  await page.waitForTimeout(4000);
  const veryQuietSamples: number[] = [];
  for (let index = 0; index < 3; index += 1) {
    const tick = Date.now();
    const response = await page.request.get(`http://127.0.0.1:${API_PORT}/api/tree?root_id=${ROOT_ID}`);
    veryQuietSamples.push(Date.now() - tick);
    expect(response.ok()).toBe(true);
  }

  // 主线程账单：长任务次数、总计、最长一次（含前 8 条的起点/时长）。
  const longTasks = await page.evaluate(() => {
    const raw = (window as unknown as { __longTasks?: { start: number; duration: number }[] }).__longTasks ?? [];
    const supported = raw.every((item) => item.start >= 0);
    const real = raw.filter((item) => item.start >= 0);
    const total = real.reduce((sum, item) => sum + item.duration, 0);
    const longest = real.reduce((max, item) => Math.max(max, item.duration), 0);
    return {
      supported,
      count: real.length,
      total_ms: total,
      longest_ms: longest,
      top: [...real].sort((a, b) => b.duration - a.duration).slice(0, 8),
    };
  });

  mkdirSync(resolve(WEB_ROOT, '.e2e'), { recursive: true });
  writeFileSync(
    ARTIFACT,
    `${JSON.stringify(
      {
        nodes: NODES,
        root_id: ROOT_ID,
        cold_ms: coldMs,
        warm_ms: warmMs,
        breakdown,
        server_tree_ms: { min: serverMs, samples },
        server_tree_ms_quiet: { min: Math.min(...quietSamples), samples: quietSamples },
        server_tree_ms_very_quiet: { min: Math.min(...veryQuietSamples), samples: veryQuietSamples },
        dom: { nodes: domNodes, edges: domEdges },
        server_metrics_tree: treeMetric,
        long_tasks: longTasks,
        target_ms: TARGET_MS,
        ratchet_ms: RATCHET_MS,
        meets_target: coldMs < TARGET_MS,
        payload_bytes: payloadBytes,
        api_port: API_PORT,
        at: new Date().toISOString(),
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );

  // **断言**：页面取树的 TTFB 不许再替"外部进程扫描"买单。
  //
  // 阈值 800ms 是量出来的分离度：修好后两次加载的较小值 239–429ms（n=8，最坏 429），
  // 把后台刷新关掉后 1032–1165ms（n=4，最好 1032）—— 800 在两者之间，两边余量都 >1.3 倍。
  //
  // ⚠️ 这条断言的第一版用的是"服务端自报 p50 < 500ms"，**变异测试证明它太弱**：
  // 把后台刷新关掉之后 p50 仍然是 450ms（因为慢的只占 1/3），判据照样绿。
  // 现在改成"两次加载里较好的一次 TTFB"，上面那个变异会红。
  expect(
    bestTreeWait,
    `页面取树 TTFB 较好的一次也有 ${bestTreeWait}ms（cold=${coldTreeWait} warm=${warmTreeWait}）` +
      ` —— 看起来又替"外部进程扫描"买单了（后台刷新在 api/app/main.py 的 _external_process_scan_loop）`,
  ).toBeLessThan(800);
  const serverP50 = Number((treeMetric?.p50_ms as number | undefined) ?? Number.NaN);
  expect(serverP50, `服务端自报 /api/tree p50=${serverP50}ms`).toBeLessThan(650);

  // 把数字打出来：pytest 的 durations 之外，这条判据的"余量"要肉眼可见。
  console.log(
    `[perf] 千节点首屏 cold=${coldMs}ms warm=${warmMs}ms（目标 ${TARGET_MS}ms，` +
      `棘轮 ${RATCHET_MS}ms，达标=${coldMs < TARGET_MS ? '是' : '否'}）` +
      `payload=${(payloadBytes / 1024).toFixed(0)}KB 服务端取树=${JSON.stringify(samples)}ms ` +
      `服务端静置=${JSON.stringify(quietSamples)}ms 服务端久静=${JSON.stringify(veryQuietSamples)}ms DOM节点=${domNodes}/边=${domEdges} ` +
      `主线程长任务=${longTasks.count} 次/共 ${longTasks.total_ms}ms/最长 ${longTasks.longest_ms}ms ` +
      `服务端自报=${JSON.stringify(treeMetric)} ` +
      `拆解=${JSON.stringify(breakdown)}`,
  );

  expect(
    coldMs,
    `千节点首屏 ${coldMs}ms 超过了棘轮 ${RATCHET_MS}ms —— 这比"没达到 ${TARGET_MS}ms 目标"更严重：` +
      `它是**变慢了**（数字与拆解在 ${ARTIFACT}）`,
  ).toBeLessThan(RATCHET_MS);
});
