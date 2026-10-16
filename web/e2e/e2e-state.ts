/**
 * E2E 的**单一真源**：状态根、库 URL、端口、后端解释器。
 *
 * 为什么单独成文件：`playwright.config.ts`（起服务）与 `perf-first-screen.spec.ts`
 * （直接往同一个库里造千节点树）必须看到**同一个**状态根与库 —— 各写一份就会漂移，
 * 而漂移的症状是"性能用例测的是另一棵空树"，它**照样会绿**。
 */
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

/** `web/` 目录（本文件在 `web/e2e/` 下）。 */
export const WEB_ROOT = resolve(import.meta.dirname, '..');

/** 刻意避开 8765/5173：那两个是本机常驻的开发实例。 */
export const API_PORT = Number(process.env.CODEX_BATTLEFIELD_E2E_API_PORT || 8877);
export const WEB_PORT = Number(process.env.CODEX_BATTLEFIELD_E2E_WEB_PORT || 5273);

export const STATE_DIR = resolve(WEB_ROOT, '.e2e', 'state');
/** sqlite URL 必须是正斜杠，且 Windows 盘符形式 `sqlite:///D:/x/y`。 */
export const DB_URL = `sqlite:///${resolve(STATE_DIR, 'e2e.sqlite3').replace(/\\/g, '/')}`;

/**
 * 后端的解释器必须是 **`api/.venv` 里那个**。
 * 实测踩过：用裸 `python` 时命中的是系统 Python 3.11，而 uvicorn 装在 3.12 的 venv 里，
 * 报 `No module named uvicorn` —— 而 Playwright 只把整个 webServer 写成
 * "not able to start, exit code 1"，看不出是解释器选错了。
 */
export function backendPython(): string {
  const root = resolve(WEB_ROOT, '..', 'api', '.venv');
  const candidates = [resolve(root, 'Scripts', 'python.exe'), resolve(root, 'bin', 'python')];
  return candidates.find((item) => existsSync(item)) ?? 'python';
}
