/**
 * 已双语化的文件清单（P2-6）。
 *
 * ## 为什么要有这份清单
 *
 * "双语"最容易**只做一半**：某个组件加了中文文案忘了英文，或者反过来。
 * 而且它是静默的 —— 中文用户看到的中文永远是对的，所以问题只会由英文用户发现，
 * 而他们通常不报告，直接走了。
 *
 * 所以覆盖范围必须**显式声明**，并由测试按声明核对（`i18n.test.ts` 的 L3）：
 *
 * * 往 `LOCALIZED` 里加文件 = 承诺"这个文件的界面文案全部走词典"；
 * * 没加 = 测试不检查它（诚实：还没转）；
 * * 加了却没转干净 = 测试**红**。
 *
 * 只声明**已经全部转好**的文件。不要为了让测试变绿而把没转完的塞进来 ——
 * 那会让这份清单从"承诺"退化成"现状描述"，测试也就没意义了。
 */
export interface LocalizedFile {
  /** 相对 `web/src/` 的路径。 */
  path: string;
  /** 为什么可以声明为"已全部转好"（供 review，不参与判断）。 */
  note: string;
}

export const LOCALIZED: LocalizedFile[] = [
  { path: 'LanguageSwitch.tsx', note: '语言开关：文案只有语言名，不随语言变化' },
  { path: 'GlossaryPanel.tsx', note: '术语表：面板文案走词典，术语内容来自 glossary.ts 的双语字段' },
  { path: 'main.tsx', note: '入口：没有任何界面文案（也顺带验证 L3 确实忽略注释）' },
];

/**
 * 还没转的文件 —— 覆盖进度必须可核对。
 *
 * 列在这里不是"以后再说"，而是为了让 `LOCALIZED` 不被误读成"整个界面都双语了"。
 * 测试会核对这份清单里的每个文件都**真实存在**：文件被删掉或改名后，
 * 清单会红，而不是悄悄烂掉。
 *
 * 顺序大致按优先级：面板（用户直接看得到）在前，`App.tsx` 最后
 * —— 它的拆分属于 P5-2，那时候一起转才不会白做一遍。
 */
export const PENDING: string[] = [
  'CostPanel.tsx',
  'CrossProjectPanel.tsx',
  'StateAdminPanel.tsx',
  'SettingsPanel.tsx',
  'RunInspector.tsx',
  'ProjectOnboarding.tsx',
  'StewardPanel.tsx',
  'App.tsx',
];
