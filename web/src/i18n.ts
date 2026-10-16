/**
 * 界面双语（P2-6）：语言状态、词典与查表。
 *
 * ## 为什么双语不只是"再加一份翻译"
 *
 * 这个工具里的关键词大多是**自造词**：债务（obligation）、验收面、宪法、休眠、
 * 前置阻塞……它们在这里有精确含义，跟日常语言里的同名词**不是一回事**。
 * 一个外部使用者把 "obligation" 读成"待办事项"、把 "acceptance" 读成"测试通过"，
 * 后面每一个判断都会错位 —— 而且不会有任何报错提示。
 *
 * 所以两件事必须成对做：
 *
 * 1. **界面可切语言**（否则非中文使用者第一步就卡住）；
 * 2. **术语表**（把自造词的中英对照与"为什么需要它"写在一处）。
 *
 * 只做 1，译文里的自造词仍然猜不出来；只做 2，没人找得到。
 *
 * ## 三条不变量（由 `i18n.test.ts` 钉住，每条都能真的红）
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | L1 | 两种语言**键完全一致** | 漏译的键静默回落成另一种语言，界面上半中半英 |
 * | L2 | 词典里**不许有空串** | 空串渲染成"这里什么都没有"，比漏译更难查 |
 * | L3 | 声明已双语的文件里**不许有字面文案** | "改了中文忘改英文"是静默的：英文用户看到中文 |
 *
 * L3 是唯一会随代码增长而失效的一条，所以它由**测试**按 `localized.ts` 的声明核对，
 * 不靠人的记性。
 */
import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { EN } from './i18n.en';
import { ZH, type DictKey } from './i18n.zh';

export type Lang = 'zh' | 'en';

export const LANGS: readonly Lang[] = ['zh', 'en'] as const;

/** 语言的自称 —— 永不用当前语言去翻译另一个语言的名字。 */
export const LANG_LABEL: Record<Lang, string> = {
  zh: '中文',
  en: 'English',
};

/** `<html lang>` 用的 BCP-47 标签。 */
export const HTML_LANG: Record<Lang, string> = {
  zh: 'zh-CN',
  en: 'en',
};

const STORAGE_KEY = 'cb.lang';

export const DICTS: Record<Lang, Record<DictKey, string>> = { zh: ZH, en: EN };

/** 某一语言下某个键的文案。键由类型保证存在，所以这里不做回落。 */
export function text(lang: Lang, key: DictKey): string {
  return DICTS[lang][key];
}

/** 拼插值：`{name}` 占位。刻意不做更复杂的模板 —— 翻译者要能看懂原文。 */
export function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in values ? String(values[name]) : whole,
  );
}

export interface I18n {
  lang: Lang;
  setLang: (next: Lang) => void;
  /** 查表；`values` 给插值。 */
  t: (key: DictKey, values?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18n | null>(null);

/** 从 localStorage 读初始语言。私密模式/测试环境下抛错就回落默认值。 */
function readStoredLang(): Lang | null {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    return raw === 'zh' || raw === 'en' ? raw : null;
  } catch {
    return null;
  }
}

function detectLang(): Lang {
  const stored = readStoredLang();
  if (stored) return stored;
  try {
    const nav = globalThis.navigator?.language ?? '';
    return nav.toLowerCase().startsWith('zh') ? 'zh' : nav ? 'en' : 'zh';
  } catch {
    return 'zh';
  }
}

/**
 * 初始语言（导出给测试用）。
 *
 * 行为契约有三条，`i18n.detect.test.ts` 逐条钉住：
 *
 * 1. **存过的优先**：用户选过一次就不再被系统语言掰回去。
 * 2. **没存过按系统语言**：`zh*` → 中文，其它**非空**语言 → 英文，
 *    空（拿不到语言）→ 中文（这是本工具的主要使用场景，默认猜中文更省事）。
 * 3. **取不到就回落，不抛错**：隐私模式下 `localStorage` 会抛 SecurityError，
 *    让整棵树渲染失败是最差的失败方向。
 */
export function defaultLang(): Lang {
  return detectLang();
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => detectLang());

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      globalThis.localStorage?.setItem(STORAGE_KEY, next);
    } catch {
      // 记不住就记不住：语言偏好不值得为它报错。
    }
  }, []);

  useEffect(() => {
    // 同步到 <html lang>：屏幕阅读器与浏览器断词都靠它。
    try {
      globalThis.document?.documentElement?.setAttribute('lang', HTML_LANG[lang]);
    } catch {
      // 非浏览器环境（测试）没有 document —— 不是错误。
    }
  }, [lang]);

  const value = useMemo<I18n>(
    () => ({
      lang,
      setLang,
      t: (key, values) => (values ? fill(DICTS[lang][key], values) : DICTS[lang][key]),
    }),
    [lang, setLang],
  );

  return createElement(I18nContext.Provider, { value }, children);
}

/**
 * 取当前语言的查表函数。
 *
 * **没有 Provider 直接抛错**，不回落中文：静默回落会让"忘了包 Provider"变成
 * "界面莫名其妙是中文"，而不是一个当场可见的失败。
 */
export function useI18n(): I18n {
  const value = useContext(I18nContext);
  if (!value) {
    throw new Error('useI18n 必须在 <I18nProvider> 内使用（界面根节点已包一层）');
  }
  return value;
}
