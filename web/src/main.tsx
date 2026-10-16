import '@xyflow/react/dist/style.css';
import './styles.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import { I18nProvider } from './i18n';

// `I18nProvider` 包在**最外层**：它要覆盖所有面板，包括未来的弹层与浮窗。
// 漏挂的后果不是"界面变中文"，而是 `useI18n` 直接抛错（有意选的失败方向）。
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </StrictMode>,
);
