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
  const url = `http://127.0.0.1:${API_PORT}/?root_id=${ROOT_ID}`;

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
      tree_urls: tree.map((item) => `${item.name.replace(/^https?:\/\/[^/]+/, '')} ${Math.round(item.duration)}ms`),
      script_requests: scripts.length,
      script_ms: Math.round(sum(scripts)),
    };
  });

  // 第二次数（热）：说明这个数字里有多少是"第一次必然要付的钱"。判据用冷的那个。
  const warmStarted = Date.now();
  await page.reload();
  await expect(page.getByLabel('L4 自动导航画布').locator('.react-flow__node').first()).toBeVisible({
    timeout: 60_000,
  });
  const warmMs = Date.now() - warmStarted;

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

  // 把数字打出来：pytest 的 durations 之外，这条判据的"余量"要肉眼可见。
  console.log(
    `[perf] 千节点首屏 cold=${coldMs}ms warm=${warmMs}ms（目标 ${TARGET_MS}ms，` +
      `棘轮 ${RATCHET_MS}ms，达标=${coldMs < TARGET_MS ? '是' : '否'}）` +
      `payload=${(payloadBytes / 1024).toFixed(0)}KB 服务端取树=${JSON.stringify(samples)}ms ` +
      `拆解=${JSON.stringify(breakdown)}`,
  );

  expect(
    coldMs,
    `千节点首屏 ${coldMs}ms 超过了棘轮 ${RATCHET_MS}ms —— 这比"没达到 ${TARGET_MS}ms 目标"更严重：` +
      `它是**变慢了**（数字与拆解在 ${ARTIFACT}）`,
  ).toBeLessThan(RATCHET_MS);
});
