/**
 * 语言检测与持久化的行为判据（P2-6）。
 *
 * 与 `i18n.test.ts` 的分工：那边查**词典与覆盖范围**（静态事实），
 * 这边查**运行行为**（检测、持久化、回落）。
 *
 * 为什么单独一个文件：`detectLang` 依赖 `navigator`/`localStorage` 这两个全局对象，
 * 需要**每个用例独立打桩**。混在 i18n.test.ts 里会让"打桩残留"变成偶发失败，
 * 而偶发失败最终会被人忽略 —— 那比没有测试更糟。
 *
 * 三条判据都能真的红：
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | D1 | 存过的语言**优先于**系统语言 | 用户每次刷新都被掰回系统语言，开关形同虚设 |
 * | D2 | 没存过时按系统语言判断 | 英文用户第一次打开就是中文，第一步就卡住 |
 * | D3 | `localStorage` 抛错（隐私模式）时**回落而不是崩** | 整棵树渲染失败，用户看到白屏 |
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { defaultLang } from './i18n';

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubLanguage(nav: string, stored: string | null): void {
  vi.stubGlobal('navigator', { language: nav });
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => (key === 'cb.lang' ? stored : null),
    setItem: () => undefined,
  });
}

describe('D1 存过的语言优先', () => {
  it('存了 en 时，即使系统语言是中文也用 en', () => {
    stubLanguage('zh-CN', 'en');
    expect(defaultLang()).toBe('en');
  });

  it('存了 zh 时，即使系统语言是英文也用 zh', () => {
    stubLanguage('en-US', 'zh');
    expect(defaultLang()).toBe('zh');
  });

  it('存了无效值时忽略它，回落到系统语言', () => {
    stubLanguage('en-US', 'klingon');
    expect(defaultLang()).toBe('en');
  });
});

describe('D2 没存过时按系统语言', () => {
  it('系统是中文 → zh', () => {
    stubLanguage('zh-CN', null);
    expect(defaultLang()).toBe('zh');
  });

  it('系统是英文 → en', () => {
    stubLanguage('en-GB', null);
    expect(defaultLang()).toBe('en');
  });

  it('系统是第三种语言 → 回落 en（而不是中文）', () => {
    // 判据写死方向：一个法语用户看英文比看中文有用。
    stubLanguage('fr-FR', null);
    expect(defaultLang()).toBe('en');
  });
});

describe('D3 localStorage 抛错时不崩', () => {
  it('getItem 抛错 → 回落到系统语言', () => {
    vi.stubGlobal('navigator', { language: 'en-US' });
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('SecurityError: localStorage is disabled');
      },
      setItem: () => undefined,
    });
    expect(defaultLang()).toBe('en');
  });

  it('完全没有 localStorage 与 navigator 时也不抛错', () => {
    vi.stubGlobal('localStorage', undefined);
    vi.stubGlobal('navigator', undefined);
    expect(['zh', 'en']).toContain(defaultLang());
  });
});
