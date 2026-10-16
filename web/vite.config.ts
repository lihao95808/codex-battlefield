import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// API 端口**必须可配**：E2E（`e2e/`）会并行起一个自己的后端（隔离库 + 隔离状态根），
// 钉死 8765 会打到开发者本机正在跑的那个 API 上 —— 那就是"测试改了你正在看的库"。
const apiPort = process.env.CODEX_BATTLEFIELD_API_PORT || '8765';

export default defineConfig({
  plugins: [react()],
  // 单元测试只跑 `src/`。`e2e/` 是 **Playwright** 的：它的 `test`/`expect` 跟 vitest 不是
  // 一回事，vitest 撞上去会报 "Playwright Test did not expect test() to be called here"
  // —— 实测 `pnpm test` 因此红过（`Test Files 1 failed | 14 passed`）。
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
  },
  server: {
    port: 5173,
    proxy: {
      '/api': `http://127.0.0.1:${apiPort}`,
    },
    watch: {
      // 编辑器/工具做**原子写**时会在源码目录里先建一个临时目录
      // （实测形如 `web/src/.api.ts.12345.<uuid>.tmpdir/api.ts.tmp`），
      // 写完立刻删掉。原生 fs watcher 会去 watch 那个马上消失的文件，
      // 在 Windows 上直接以 `EBUSY: resource busy or locked, watch ...` **崩掉整个
      // dev server**（本项目实测被这个打断过两次，两次都是"改完文件保存后
      // Vite 就没了"，看起来像改坏了代码，实际跟代码无关）。
      //
      // 所以：忽略这些临时目录，并改用**轮询**。轮询慢一点，但不会因为
      // 短命文件而让整个服务器挂掉 —— 开发体验的稳定性比几十毫秒的
      // 重载延迟重要得多。
      ignored: ['**/.*.tmpdir/**', '**/*.tmp', '**/node_modules/**', '**/.git/**'],
      usePolling: true,
      interval: 300,
    },
  },
});
