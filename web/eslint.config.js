import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'playwright-report', 'test-results', '.e2e'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-hooks/set-state-in-effect': 'off',
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // A leading underscore marks a parameter that exists only to document a mocked signature.
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
  {
    // 构建/测试工具跑在 **Node** 里（`vite.config.ts`、`playwright.config.ts`、`e2e/*`）：
    // 那里用 `process.env` 是正常的。不给这一组声明 Node 全局，`no-undef` 会把它们判成错误
    // —— 实测 `pnpm lint` 因此红过 4 条，而 build/test 都是绿的，很容易被当成噪声忽略。
    files: ['*.config.ts', 'e2e/**/*.ts', '*.mjs'],
    languageOptions: {
      globals: { process: 'readonly', console: 'readonly' },
    },
  },
);
