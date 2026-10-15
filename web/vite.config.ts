import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8765',
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
