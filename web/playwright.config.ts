/**
 * Playwright 配置（P5-3：真浏览器 E2E）。
 *
 * ## 为什么是"两个 webServer"，而不是 mock 掉 /api
 *
 * 把 `/api` 用 `page.route` 拦住、回放假数据，测的是"前端在给定 JSON 下的行为"——
 * 那种测试**证明不了**"界面能用"，因为它恰好绕过了最容易坏的那一段：
 * 前后端字段名/形状一旦漂移，回放数据的测试永远绿。
 *
 * 所以这里起两个真进程：
 *   1. 后端 `uvicorn app.main:app`，**自己的端口 + 自己的库 + 自己的状态根**
 *      （`CODEX_BATTLEFIELD_STATE_HOME` 指向 `web/.e2e/state`），跑 `RUNNER=mock`；
 *   2. 前端 `vite`，代理指向上面那个后端（靠 `CODEX_BATTLEFIELD_API_PORT` 传进去）。
 *
 * 这样每次跑都是干净状态，也不会碰到开发者本机在 8765 上跑着的那个实例。
 *
 * ## 浏览器用完整 chromium，不用 headless shell
 *
 * 本机受限网络下 `playwright install chromium` **只下到了完整包**，headless shell
 * 那个包下到一半超时。`channel: 'chromium'` = 完整浏览器 + 新 headless 模式，
 * 实测能起来（`web/_browser_probe.mjs` 打印 `{"ok":true,...}`，156.0.8078.4）。
 */
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

import { defineConfig, devices } from '@playwright/test';

// 刻意避开 8765/5173：那两个是本机常驻的开发实例。
const API_PORT = Number(process.env.CODEX_BATTLEFIELD_E2E_API_PORT || 8877);
const WEB_PORT = Number(process.env.CODEX_BATTLEFIELD_E2E_WEB_PORT || 5273);

const STATE_DIR = resolve(import.meta.dirname, '.e2e', 'state');
// sqlite URL 必须是正斜杠，且 Windows 盘符形式 `sqlite:///D:/x/y`。
const DB_URL = `sqlite:///${resolve(STATE_DIR, 'e2e.sqlite3').replace(/\\/g, '/')}`;

/**
 * 后端的解释器必须是 **`api/.venv` 里那个**。
 *
 * 实测踩过：用裸 `python` 时命中的是系统 Python 3.11，而 uvicorn 装在 3.12 的 venv 里，
 * 报 `No module named uvicorn` —— 而 Playwright 只把整个 webServer 写成
 * "not able to start, exit code 1"，看不出是解释器选错了。
 */
function backendPython(): string {
  const root = resolve(import.meta.dirname, '..', 'api', '.venv');
  const candidates = [resolve(root, 'Scripts', 'python.exe'), resolve(root, 'bin', 'python')];
  return candidates.find((item) => existsSync(item)) ?? 'python';
}

export default defineConfig({
  testDir: './e2e',
  // 一个 E2E 会往自己的库里写任务树：并行会互相干扰（同一个库）。
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  timeout: 180_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    locale: 'zh-CN',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], channel: 'chromium' },
    },
  ],
  webServer: [
    {
      command: `"${backendPython()}" -m uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: '../api',
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 180_000,
      env: {
        CODEX_BATTLEFIELD_RUNNER: 'mock',
        CODEX_BATTLEFIELD_STATE_HOME: STATE_DIR,
        CODEX_BATTLEFIELD_DATABASE_URL: DB_URL,
      },
    },
    {
      command: `pnpm dev --port ${WEB_PORT} --strictPort`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 180_000,
      env: {
        CODEX_BATTLEFIELD_API_PORT: String(API_PORT),
      },
    },
  ],
});
