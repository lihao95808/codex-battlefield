/**
 * 用 TypeScript 自己的解析器找 `App.tsx` 里的顶层声明（P5-2 的拆分工具）。
 *
 * ## 为什么不手写扫描器
 *
 * 第一版是手写的"字符配平"扫描器，实测三次都错，而且错法一个比一个隐蔽：
 *
 * 1. 逐行扫 → 模板串 `` `${x} 小时` `` 里的 `}` 被当成块结尾（`App()` 被判成 500 行结束）；
 * 2. 改成跨行状态机 → 模板串里的 `}` 仍然被当成"不匹配的括号"；
 * 3. 修好模板串 → "从声明找第一个配平的 `}`"找的是**体内第一个块**的结尾
 *    （`WatchdogPanel` 被判成 1965 行结束，真实 2371）。
 *
 * 结论：这类活不该手写。`typescript` 本来就是这个仓库的依赖（`pnpm build` 就靠它），
 * 直接问它要 AST 是**精确**的，也省掉一整套维护成本。
 *
 * 用法（由 `_extract_component.py` 调用）：
 *
 *     node _ts_extract.mjs list
 *     node _ts_extract.mjs extract WatchdogPanel
 *
 * 输出是一个 JSON。`extract` 额外给出：组件源码、它引用到的 import 项、删除该段后的
 * 新 `App.tsx` 全文 —— **写文件由 Python 那侧做**，这个脚本只读。
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = join(HERE, 'src', 'App.tsx');

const SOURCE = readFileSync(APP, 'utf8');
const SF = ts.createSourceFile(APP, SOURCE, ts.ScriptTarget.Latest, /* setParentNodes */ true, ts.ScriptKind.TSX);

/** 顶层声明：名字、语句起止（行）、来源文本。 */
function topLevelDeclarations() {
  const out = [];
  for (const statement of SF.statements) {
    const name = declarationName(statement);
    if (!name) continue;
    const start = SF.getLineAndCharacterOfPosition(statement.getStart(SF)).line + 1;
    const end = SF.getLineAndCharacterOfPosition(statement.getEnd()).line + 1;
    out.push({ name, start, end, text: SOURCE.slice(statement.getStart(SF), statement.getEnd()) });
  }
  return out;
}

function declarationName(statement) {
  const mods = ts.canHaveModifiers(statement) ? ts.getModifiers(statement) : undefined;
  const exported = (mods ?? []).some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
  if (ts.isFunctionDeclaration(statement) && statement.name) {
    return exported ? `export function ${statement.name.text}` : statement.name.text;
  }
  if (ts.isVariableStatement(statement)) {
    for (const decl of statement.declarationList.declarations) {
      if (ts.isIdentifier(decl.name)) return decl.name.text;
    }
  }
  if (ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement)) {
    return statement.name.text;
  }
  return null;
}

/** `App.tsx` 里每个具名 import 的符号 → 模块说明符。 */
function importSymbols() {
  const map = new Map();
  for (const statement of SF.statements) {
    if (!ts.isImportDeclaration(statement)) continue;
    const module = statement.moduleSpecifier.text;
    const clause = statement.importClause;
    const bindings = clause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const element of bindings.elements) {
      map.set(element.name.text, module);
    }
  }
  return map;
}

function lines() {
  return SOURCE.split('\n');
}

function snippetFor(name) {
  const declaration = topLevelDeclarations().find((item) => item.name === name.split(' ').pop());
  if (!declaration) throw new Error(`找不到顶层声明 ${name}`);
  return declaration;
}

/** 这段代码里出现（且不是它自己声明的）的 import 符号，按模块分组。 */
function referencedImports(snippet, selfName) {
  const symbols = importSymbols();
  const grouped = new Map();
  for (const [symbol, module] of symbols) {
    if (symbol === selfName) continue;
    // 用词边界匹配：`Stat` 不该被 `StateAdminPanel` 命中。
    const pattern = new RegExp(String.raw`\b${symbol.replace(/[$]/g, '\\$&')}\b`);
    if (!pattern.test(snippet)) continue;
    if (!grouped.has(module)) grouped.set(module, new Set());
    grouped.get(module).add(symbol);
  }
  return grouped;
}

function renderImports(grouped, component) {
  const modules = [...grouped.keys()].sort();
  if (modules.length === 0) return '';
  const header = `/**\n * 从 \`App.tsx\` 拆出来的 \`${component}\`（P5-2：单文件不再超 500 行）。\n */\n`;
  const body = modules
    .map((module) => `import { ${[...grouped.get(module)].sort().join(', ')} } from '${module}';`)
    .join('\n');
  return `${header}${body}\n\n`;
}

function commandList() {
  return { file: APP, declarations: topLevelDeclarations().map(({ name, start, end }) => ({ name, start, end })) };
}

function commandExtract(component) {
  const declaration = snippetFor(component);
  const all = lines();
  const before = all.slice(0, declaration.start - 1);
  const after = all.slice(declaration.end);
  const rest = [...before, `import { ${component} } from './${component}';`, ...after];
  const snippet = all.slice(declaration.start - 1, declaration.end).join('\n');
  const grouped = referencedImports(snippet, component);
  return {
    component,
    start: declaration.start,
    end: declaration.end,
    snippet,
    imports: renderImports(grouped, component),
    grouped: Object.fromEntries([...grouped].map(([module, names]) => [module, [...names].sort()])),
    app: rest.join('\n'),
    appLinesBefore: all.length,
    appLinesAfter: rest.length,
  };
}

const [command, argument] = process.argv.slice(2);
let result;
if (command === 'list') {
  result = commandList();
} else if (command === 'extract') {
  result = commandExtract(argument);
} else {
  console.error('用法：node _ts_extract.mjs list|extract <Name>');
  process.exit(2);
}
process.stdout.write(JSON.stringify(result, null, 2));
