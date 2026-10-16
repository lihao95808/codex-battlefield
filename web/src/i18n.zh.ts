/**
 * 中文词典 —— **唯一真源**（P2-6）。
 *
 * 键名规矩：`<命名空间>.<名字>`，或 `<命名空间>.<组>.<名字>`（如 `glossary.col.*`）。
 * 命名空间与组件一一对应；`i18n.test.ts` 的 L4 会核对命名空间是否已登记。
 *
 * `EN` 的类型就是这份对象的键集合，所以这里漏加/多加键都会在**编译期**报错。
 *
 * 写文案时的两条经验（都来自这个仓库自己踩过的坑）：
 *
 * * **说清代价，不要只说状态**。"未绑定"不够，要写"不绑也能用，但状态会退回仓库内 workspace"。
 * * **不要用恒真的词**。"运行正常"在界面里等于没说；要么给数字，要么给下一步动作。
 */
export const ZH = {
  // ---------- 语言开关 ----------
  'lang.label': '语言',
  'lang.switchTo.zh': '切换到中文',
  'lang.switchTo.en': 'Switch to English',

  // ---------- 术语表 ----------
  'glossary.title': '术语表',
  'glossary.lead':
    '这里的词大多是本工具自造、或**改写过含义**的。按日常意思理解它们，后面每一步判断都会错位。',
  'glossary.col.interop': '跟日常用法的差别（这张表的重点）',
  'glossary.note':
    '完整定义见仓库 docs/ 目录下的 SPEC。本表只解释"它跟日常用法的差别"，不替代 SPEC。',
} as const;

/** 词典的键集合。`EN` 必须与它一致 —— 类型与 `i18n.test.ts` 各挡一次。 */
export type DictKey = keyof typeof ZH;

/** 把所有键列出来（测试按它核对 `EN`，也用它做命名空间检查）。 */
export const ZH_KEYS = Object.keys(ZH) as DictKey[];
