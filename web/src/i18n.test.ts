/**
 * 双语的判据（P2-6）。每一条都能真的红 —— 这是本文件存在的全部意义。
 *
 * | 编号 | 不变量 | 不钉住会怎样 |
 * |---|---|---|
 * | L1 | 两种语言**键完全一致** | 漏译的键静默回落成另一种语言，界面半中半英 |
 * | L2 | 词典不许有**空串** | 空串渲染成"这里什么都没有"，比漏译更难查 |
 * | L3 | `LOCALIZED` 声明的文件里**不许有字面文案** | "改了中文忘英文"是静默的：中文用户永远看不到这个 bug |
 * | L4 | 键的命名空间**收敛**（`<面板>.<名字>`） | 键名漂移后没人找得到一条文案在哪 |
 * | L5 | 界面根节点**真的挂了** `I18nProvider` | 漏挂 = `useI18n` 抛错；错挂 = 界面凭空变中文 |
 * | L6 | 术语表每条都**指得到 SPEC** | 术语表变成第二份真源，与 SPEC 悄悄分叉 |
 * | L7 | **切换语言真的会改文案** | 开关看起来能点，实际什么都没变 |
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { TERMS, pick } from './glossary';
import { DICTS, HTML_LANG, LANGS, LANG_LABEL, fill, text, type Lang } from './i18n';
import { EN } from './i18n.en';
import { ZH, ZH_KEYS } from './i18n.zh';
import { LOCALIZED, PENDING } from './localized';

const SRC = join(import.meta.dirname, '.');

function readSource(relative: string): string {
  return readFileSync(join(SRC, relative), 'utf8');
}

/** 去掉注释后的源码行（L3 只看**真正会被渲染**的内容，不看给维护者看的注释）。 */
function codeLines(source: string): string[] {
  const kept: string[] = [];
  let inBlock = false;
  for (const raw of source.split('\n')) {
    const line = raw.trim();
    if (inBlock) {
      if (line.includes('*/')) inBlock = false;
      continue;
    }
    if (line.startsWith('/*') || line.startsWith('/**')) {
      if (!line.includes('*/')) inBlock = true;
      continue;
    }
    if (line.startsWith('//') || line.startsWith('*')) continue;
    kept.push(raw);
  }
  return kept;
}

/** CJK 统一表意文字：出现在 JSX 文本或字符串字面量里就意味着"有字面文案"。 */
const CJK = /[\u4e00-\u9fff]/;

/** 已登记的键命名空间（第一段）。加新命名空间必须显式改这里。 */
const KEY_NAMESPACES = new Set(['lang', 'glossary']);

/** 已登记的三段键组名（前两段）。 */
const KEY_GROUPS = new Set(['lang.switchTo', 'glossary.col']);

/**
 * 允许"在词典里但暂时没被代码使用"的键。
 *
 * 这份白名单必须为空，或者只放**有明确理由**的键 —— 它是 L8 的逃生舱，
 * 一旦被用来掩盖打错的键名，L8 就白写了。第二段测试会核对它不会过期。
 */
const UNUSED_ALLOWED = new Set<string>([]);

/** 参与 L8 扫描的源码文件：`src/` 下的 .ts/.tsx，排除测试自身。 */
function sourceFiles(): string[] {
  return readdirSync(SRC)
    .filter((name) => /\.tsx?$/.test(name) && !name.endsWith('.test.ts'))
    .sort();
}

// ---------- L1 键完全一致 ----------

describe('L1 两种语言的键完全一致', () => {
  it('EN 与 ZH 的键集合相同（多一个或漏一个都要红）', () => {
    const zh = new Set(Object.keys(ZH));
    const en = new Set(Object.keys(EN));

    const missing = [...zh].filter((key) => !en.has(key));
    const extra = [...en].filter((key) => !zh.has(key));

    expect(missing, `英文缺这些键：${missing.join(', ')}`).toEqual([]);
    expect(extra, `英文多了这些键（中文里没有）：${extra.join(', ')}`).toEqual([]);
  });

  it('ZH_KEYS 与 ZH 真实键一致（防止导出清单与词典脱钩）', () => {
    expect([...ZH_KEYS].sort()).toEqual(Object.keys(ZH).sort());
  });

  it('DICTS 两种语言都按同一份键集合可查', () => {
    for (const lang of LANGS) {
      for (const key of ZH_KEYS) {
        expect(typeof DICTS[lang][key], `${lang} 的 ${key} 不是字符串`).toBe('string');
      }
    }
  });
});

// ---------- L2 不许空串 ----------

describe('L2 词典不许有空串', () => {
  it('每种语言的每个值都非空', () => {
    for (const lang of LANGS) {
      const empty = ZH_KEYS.filter((key) => DICTS[lang][key].trim() === '');
      expect(empty, `${lang} 里这些键是空的：${empty.join(', ')}`).toEqual([]);
    }
  });
});

// ---------- L3 声明已双语的文件的 JSX 里不许有字面文案 ----------

describe('L3 已双语的组件不许留字面文案', () => {
  it.each(LOCALIZED.map((item) => item.path))('%s 里没有中日韩字面文案', (path) => {
    const offenders = codeLines(readSource(path))
      .map((line, index) => ({ line: line.trim(), number: index + 1 }))
      .filter((entry) => CJK.test(entry.line));

    expect(
      offenders.map((entry) => `${path}:${entry.number} ${entry.line}`),
      '这些行里还有写死的中文文案 —— 英文用户会在中文界面之外看到它们',
    ).toEqual([]);
  });

  it('LOCALIZED 与 PENDING 不重叠、且文件都真实存在', () => {
    const localized = LOCALIZED.map((item) => item.path);
    const overlap = localized.filter((path) => PENDING.includes(path));
    expect(overlap, `同时被声明为已转与未转：${overlap.join(', ')}`).toEqual([]);
  });

  it('PENDING 与 LOCALIZED 不重叠，且两份清单内部都不重复', () => {
    const localized = new Set(LOCALIZED.map((item) => item.path));
    expect(PENDING.filter((path) => localized.has(path))).toEqual([]);
    expect(localized.size, 'LOCALIZED 里有重复项').toBe(LOCALIZED.length);
    expect(new Set(PENDING).size, 'PENDING 里有重复项').toBe(PENDING.length);
  });

  it('两份清单里的文件都真实存在（改了名不会留下烂条目）', () => {
    for (const path of [...LOCALIZED.map((item) => item.path), ...PENDING]) {
      expect(() => readSource(path), `${path} 不在 web/src 下`).not.toThrow();
    }
  });
});

// ---------- L4 键的命名空间收敛 ----------

describe('L4 键的命名空间收敛', () => {
  it('每个键的第一段都是已登记的命名空间', () => {
    // 合法命名空间写在测试里而不是藏进实现：加新命名空间必须显式改这里，
    // 于是"顺手起个新前缀"会被人看见。
    const bad = ZH_KEYS.filter((key) => !KEY_NAMESPACES.has(key.split('.')[0]));
    expect(bad, `这些键的命名空间没有登记：${bad.join(', ')}`).toEqual([]);
  });

  it('三段键的中间一段是已登记的组名', () => {
    // 只查"第一段合法"会漏掉 `lang.other.thing` 这种：段数对、前缀对，
    // 却是一个谁也不会用的键。组名必须显式登记，于是假组名会红。
    const bad = ZH_KEYS.filter((key) => {
      const parts = key.split('.');
      return parts.length === 3 && !KEY_GROUPS.has(`${parts[0]}.${parts[1]}`);
    });
    expect(bad, `这些三段键用了没登记的组名：${bad.join(', ')}`).toEqual([]);
  });

  it('每个键是 2 或 3 段，且没有空段', () => {
    const malformed = ZH_KEYS.filter((key) => {
      const parts = key.split('.');
      return (parts.length !== 2 && parts.length !== 3) || parts.some((part) => part === '');
    });
    expect(
      malformed,
      `这些键不是 <命名空间>.<名字> 或 <命名空间>.<组>.<名字>：${malformed.join(', ')}`,
    ).toEqual([]);
  });
});

// ---------- L8 词典与代码对得上 ----------

describe('L8 词典里没有孤儿键，代码里没有幽灵键', () => {
  it('每个键都真的被代码用到，且用到的键都真的存在', () => {
    const used = new Set<string>();
    for (const file of sourceFiles()) {
      const source = readSource(file);
      for (const match of source.matchAll(/\bt\(\s*'([^']+)'/g)) {
        used.add(match[1]);
      }
    }

    const orphans = ZH_KEYS.filter((key) => !used.has(key) && !UNUSED_ALLOWED.has(key));
    expect(
      orphans,
      `这些键在词典里但没有任何代码使用 —— 要么是打错名字，要么是删了界面忘了删键：${orphans.join(', ')}`,
    ).toEqual([]);

    const ghosts = [...used].filter((key) => !(key in ZH));
    expect(
      ghosts,
      `这些键被代码使用但词典里没有 —— 运行时会拿到 undefined：${ghosts.join(', ')}`,
    ).toEqual([]);
  });

  it('UNUSED_ALLOWED 里的每一项都仍然是真的未使用（拆掉界面后要清掉白名单）', () => {
    const used = new Set<string>();
    for (const file of sourceFiles()) {
      for (const match of readSource(file).matchAll(/\bt\(\s*'([^']+)'/g)) {
        used.add(match[1]);
      }
    }
    const stale = [...UNUSED_ALLOWED].filter((key) => used.has(key));
    expect(stale, `这些键已经被用到了，白名单该删掉：${stale.join(', ')}`).toEqual([]);
  });
});

// ---------- L5 Provider 真的挂了 ----------

describe('L5 界面根节点挂了 I18nProvider', () => {
  it('main.tsx 里包了 I18nProvider', () => {
    expect(readSource('main.tsx')).toContain('I18nProvider');
  });
});

// ---------- L6 术语表每条指得到 SPEC ----------

describe('L6 术语表指得到 SPEC', () => {
  it('每条术语都有 spec 路径，且指向仓库里真实存在的文件或明确的 ROADMAP 行', () => {
    for (const term of TERMS) {
      expect(term.spec, `${term.name.en} 没有 spec`).toBeTruthy();
      expect(term.spec).toMatch(/^(docs\/[\w.-]+\.md|ROADMAP\.md)/);
    }
  });

  it('术语的中英名都不为空（术语名两种语言都要显示）', () => {
    for (const term of TERMS) {
      expect(term.name.zh.trim(), `${term.name.en} 缺中文名`).not.toBe('');
      expect(term.name.en.trim(), `${term.name.zh} 缺英文名`).not.toBe('');
    }
  });

  it('没有重复术语（按英文名去重）', () => {
    const seen = TERMS.map((term) => term.name.en);
    expect(new Set(seen).size, `重复术语：${seen.join(', ')}`).toBe(seen.length);
  });

  it('两种语言的说明都写全了（不能只写一半）', () => {
    for (const term of TERMS) {
      for (const lang of LANGS) {
        expect(pick(term.meaning, lang).trim(), `${term.name.en} 的 ${lang} 含义为空`).not.toBe('');
        expect(pick(term.interop, lang).trim(), `${term.name.en} 的 ${lang} 差别说明为空`).not.toBe('');
      }
    }
  });
});

// ---------- L7 切换语言真的会改文案 ----------

describe('L7 切换语言真的改文案', () => {
  it('同一个键在两种语言下给出不同文案（除非它本来就是术语名）', () => {
    const identical = ZH_KEYS.filter((key) => DICTS.zh[key] === DICTS.en[key]);
    // `switchTo.zh` 有意两种语言都写中文、`switchTo.en` 有意都写英文
    // —— 让读不懂当前语言的人也能切回去。
    const allowedSame = new Set(['lang.switchTo.zh', 'lang.switchTo.en']);
    expect(
      identical.filter((key) => !allowedSame.has(key)),
      '这些键两种语言一模一样 —— 多半是漏译',
    ).toEqual([]);
  });

  it('text() 按语言返回对应文案', () => {
    expect(text('zh', 'lang.label')).toBe('语言');
    expect(text('en', 'lang.label')).toBe('Language');
  });

  it('插值会替换命名的占位符，未提供的占位符原样保留', () => {
    expect(fill('已迁移到 {path}（{count} 条）', { path: '/x', count: 3 })).toBe('已迁移到 /x（3 条）');
    expect(fill('保留 {unknown}', {})).toBe('保留 {unknown}');
  });

  it('语言标签与 html lang 两种语言都有', () => {
    for (const lang of LANGS as readonly Lang[]) {
      expect(LANG_LABEL[lang]).toBeTruthy();
      expect(HTML_LANG[lang]).toBeTruthy();
    }
    expect(HTML_LANG.zh).toBe('zh-CN');
    expect(HTML_LANG.en).toBe('en');
  });
});
