/**
 * 把 `App.tsx` 里的顶层声明抽到独立文件里（P5-2 的拆分工具）。
 *
 * ## 为什么用 TypeScript 自己的解析器，而不是手写扫描器
 *
 * 第一版是手写的"字符配平"扫描器，实测连错三次，错法一个比一个隐蔽：
 *
 * 1. 逐行扫 → 模板串 `` `${x} 小时` `` 里的 `}` 被当成块结尾（`App()` 被判成 500 行结束）；
 * 2. 改成跨行状态机 → 模板串里的 `}` 仍被当成"不匹配的括号"；
 * 3. 修好模板串 → "从声明找第一个配平的 `}`"找到的是**体内第一个块**的结尾
 *    （`WatchdogPanel` 被判成 1965 行结束，真实 2371）。
 *
 * 这类活不该手写：`typescript` 本来就是这个仓库的依赖（`pnpm build` 靠它）。
 *
 * ## 命令
 *
 *     node _ts_extract.mjs list
 *         → 顶层声明与行范围
 *
 *     node _ts_extract.mjs plan <OutFile.ts> <Name> <Name> ... [-- <OutFile2.tsx> ...]
 *         → 抽出计划：每个输出文件的内容（含需要的 import）、以及删除这些块之后的新
 *           `App.tsx`。**只输出，不写盘** —— 写盘由 `_apply_extract.py` 做，
 *           这样"算"和"改"分开，出错了 `git checkout -- web/src/App.tsx` 就能回退。
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import ts from 'typescript';

const HERE = dirname(fileURLToPath(import.meta.url));
/** 目标文件：默认 `src/App.tsx`；`list --file src/panels.tsx` 可以指定别的。 */
function resolveTarget(args) {
  const index = args.indexOf('--file');
  if (index === -1) return join(HERE, 'src', 'App.tsx');
  return join(HERE, args[index + 1]);
}

const RAW_ARGS = process.argv.slice(2);
const TARGET = resolveTarget(RAW_ARGS);
const CLEAN_ARGS = RAW_ARGS.filter((_, index) => {
  if (RAW_ARGS[index - 1] === '--file') return false;
  return RAW_ARGS[index] !== '--file';
});

const SOURCE = readFileSync(TARGET, 'utf8');
const SF = ts.createSourceFile(TARGET, SOURCE, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const LINES = SOURCE.split('\n');

/** 顶层声明：名字、起止行、来源文本。 */
function declarations() {
  const out = [];
  for (const statement of SF.statements) {
    const name = declarationName(statement);
    if (!name) continue;
    const start = SF.getLineAndCharacterOfPosition(statement.getStart(SF)).line + 1;
    const end = SF.getLineAndCharacterOfPosition(statement.getEnd()).line + 1;
    out.push({ name, start, end });
  }
  return out;
}

const DECLS = declarations();
const BY_NAME = new Map(DECLS.map((item) => [item.name, item]));

function declarationName(statement) {
  if (ts.isFunctionDeclaration(statement) && statement.name) return statement.name.text;
  if (ts.isVariableStatement(statement)) {
    for (const decl of statement.declarationList.declarations) {
      if (ts.isIdentifier(decl.name)) return decl.name.text;
    }
  }
  if (ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement)) return statement.name.text;
  return null;
}

/** `App.tsx` 里每个具名 import 的符号 → 模块说明符。 */
function importSymbols() {
  const map = new Map();
  for (const statement of SF.statements) {
    if (!ts.isImportDeclaration(statement)) continue;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const element of bindings.elements) map.set(element.name.text, statement.moduleSpecifier.text);
  }
  return map;
}

const IMPORTS = importSymbols();

/** 该名字的源码文本（含声明本身）。**去掉原来的 `export`**，由 `blockText` 统一加回来。 */
function blockText(name) {
  const item = BY_NAME.get(name);
  if (!item) throw new Error(`找不到顶层声明 ${name}`);
  const lines = LINES.slice(item.start - 1, item.end).map((line) => line.replace(/^export\s+/, ''));
  return lines.join('\n');
}

/** 顶层声明统一 `export`：搬出去的东西被别的文件 import，不导出就报 `TS2459`（实测）。 */
function exportTopLevel(text) {
  return text.replace(/^(function|const|let|type|interface|class)\s/gm, 'export $1 ');
}

/** 文本里出现的 import 符号（排除 `excluded` 与它自己声明的名字）。
 *
 * `extraModules` 是"**本次一起搬出来的兄弟文件**"：`symbol -> [模块说明符, ...]`。
 * 没有它，`panels.tsx` 里对 `numberValue` / `Section` 的引用会被当成"本地找不到"，
 * 报一串 `TS2304`（实测第一版就是这样）。一个符号可能有多个来源，所以给的是列表。
 */
function neededImports(text, excluded, extraModules) {
  const declared = new Set();
  for (const match of text.matchAll(/^(?:export\s+)?(?:function|const|type|interface)\s+([A-Za-z_][A-Za-z0-9_]*)/gm)) {
    declared.add(match[1]);
  }
  // 候选来源：本次搬出去的兄弟文件优先（那个路径一定对），再补原来的 import 表。
  // **不要**在这里排除 `excluded`：`excluded` 是"组内名字"，而 `numberValue` 这种
  // "搬到 taskView.ts、被 panels.tsx 用"的符号**同时**在那张表里 —— 一起排掉就会报
  // `TS2304`（实测第一版就是这样）。组内名字由 `declared` 与自指模块两条来挡。
  const candidates = new Map();
  for (const [symbol, modules] of extraModules) candidates.set(symbol, [...modules]);
  for (const [symbol, module] of IMPORTS) {
    if (!candidates.has(symbol)) candidates.set(symbol, []);
    candidates.get(symbol).push(module);
  }

  const grouped = new Map();
  for (const [symbol, modules] of candidates) {
    if (declared.has(symbol)) continue;
    const pattern = new RegExp(String.raw`\b${symbol.replace(/[$]/g, '\\$&')}\b`);
    if (!pattern.test(text)) continue;
    for (const module of modules) {
      if (!grouped.has(module)) grouped.set(module, new Set());
      grouped.get(module).add(symbol);
    }
  }
  return grouped;
}

function renderImports(grouped, title) {
  const modules = [...grouped.keys()].sort();
  if (modules.length === 0) return '';
  const header =
    `/**\n * 从 \`App.tsx\` 拆出来的：${title}（P5-2：单文件不再超 500 行）。\n *\n` +
    ` * 抽出来是为了让 \`App.tsx\` 回到 500 行以内；这里只搬家，没有改行为。\n */\n`;
  const body = modules
    .map((module) => `import { ${[...grouped.get(module)].sort().join(', ')} } from '${module}';`)
    .join('\n');
  return `${header}${body}\n\n`;
}

/** 解析 `plan` 的参数：`Out.ts Name Name -- Out2.tsx Name ...`。 */
function parsePlanArgs(args) {
  const groups = [];
  let current = null;
  for (const item of args) {
    if (item === '--') {
      current = null;
      continue;
    }
    if (/\.tsx?$/.test(item)) {
      current = { out: item, names: [] };
      groups.push(current);
      continue;
    }
    if (!current) throw new Error(`第一个参数必须是输出文件名（.ts/.tsx），收到 ${item}`);
    current.names.push(item);
  }
  return groups;
}

function commandPlan(args) {
  const groups = parsePlanArgs(args);
  const allNames = new Set(groups.flatMap((group) => group.names));
  // 兄弟文件表：本次一起搬出来的符号 → 它现在住哪个文件（从当前文件出发的相对路径）。
  const extraModules = new Map();
  for (const group of groups) {
    const module = `./${group.out.replace(/\.tsx?$/, '')}`;
    for (const name of group.names) {
      if (!extraModules.has(name)) extraModules.set(name, []);
      extraModules.get(name).push(module);
    }
  }
  const files = {};
  const removed = [];
  for (const group of groups) {
    const selfModule = `./${group.out.replace(/\.tsx?$/, '')}`;
    const parts = group.names.map((name) => exportTopLevel(blockText(name)));
    const text = parts.join('\n\n');
    // 自己文件里的符号不算"要 import 的"；指向自己的兄弟条目也剔掉。
    const rel = new Map(
      [...extraModules].map(([symbol, modules]) => [symbol, modules.filter((module) => module !== selfModule)]),
    );
    const grouped = neededImports(text, allNames, rel);
    const label = group.names.length === 1 ? group.names[0] : `${group.names.length} 个声明`;
    files[group.out] = { content: renderImports(grouped, label) + text + '\n', names: group.names };
    for (const name of group.names) removed.push(name);
  }

  // 删除这些块，并在 App.tsx 的 import 区补上对新文件的 import。
  const cut = new Set();
  for (const name of removed) {
    const item = BY_NAME.get(name);
    for (let line = item.start; line <= item.end; line += 1) cut.add(line);
  }
  const kept = LINES.filter((_, index) => !cut.has(index + 1));
  const keptText = kept.join('\n');
  const newImports = groups
    .map((group) => {
      const modulePath = `./${group.out.replace(/\.tsx?$/, '')}`;
      // 只把"App 里还真的用到"的符号 import 回来。纯内部使用的块（比如 `nodeTypes`
      // 只被 `TaskNodeView` 用，而两者被搬到同一个文件里）不该在这里多一行 import ——
      // 多一行就会让 `tsc` 报未使用，或者把两个文件拖成循环引用。
      const symbols = group.names.filter((name) => new RegExp(String.raw`\b${name}\b`).test(keptText));
      return symbols.length ? { modulePath, symbols } : null;
    })
    .filter(Boolean);
  const importBlock = newImports.map((item) => `import { ${item.symbols.join(', ')} } from '${item.modulePath}';`).join('\n');
  // **插在第一个 import 之前**，而不是"最后一个 import 之后"：后者实测把新行插进了
  // 一个多行 `import type { ... }` 的中间（报 `TS1003 Identifier expected`）。
  // 插在最前面永远安全，而且 import 顺序本来就不影响语义（只是 lint 想按字母排，
  // 这一组是本次新加的，后面再统一排）。
  const firstImport = kept.findIndex((line) => line.startsWith('import '));
  const insertAt = importBlock ? (firstImport === -1 ? 0 : firstImport) : 0;
  const newApp = importBlock
    ? [...kept.slice(0, insertAt), importBlock, ...kept.slice(insertAt)].join('\n')
    : kept.join('\n');

  return {
    files,
    removed,
    app: newApp,
    appLinesBefore: LINES.length,
    appLinesAfter: newApp.split('\n').length,
  };
}

function commandList() {
  return { file: TARGET, declarations: DECLS, totalLines: LINES.length };
}

/** `identifiers` 子命令：某个行范围内出现的标识符（去掉属性名/对象键/字符串）。 */
function commandIdentifiers(fromLine, toLine) {
  const start = SF.getPositionOfLineAndCharacter(fromLine - 1, 0);
  const endLineText = LINES[toLine - 1] ?? '';
  const end = SF.getPositionOfLineAndCharacter(toLine - 1, endLineText.length);
  const found = new Set();
  const visit = (node) => {
    if (node.getStart(SF) >= start && node.getEnd() <= end && ts.isIdentifier(node)) {
      const parent = node.parent;
      const isPropertyName =
        (ts.isPropertyAccessExpression(parent) && parent.name === node) ||
        (ts.isPropertyAssignment(parent) && parent.name === node) ||
        (ts.isJsxAttribute(parent) && parent.name === node) ||
        (ts.isBindingElement(parent) && parent.name === node);
      if (!isPropertyName) found.add(node.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(SF);
  return { from: fromLine, to: toLine, identifiers: [...found].sort() };
}

/** `rename` 子命令：在指定行范围内给一批标识符加前缀（`view.`）。
 *
 * 这是"把组件体内的逻辑搬进 hook、JSX 里改用 `view.xxx`"那一步的机械部分。
 * 只改**纯标识符引用**：`ident(`、`ident.`、`ident}`、`{ident}`、`ident &&` …
 * 不碰属性名（`a.ident`）、对象键（`ident:`）、字符串与 JSX 属性名。
 */
function commandRename(fromLine, toLine, outFile, ...names) {
  const wanted = new Set(names);
  let deleteFrom = null;
  let deleteTo = null;
  const marker = names.indexOf('--delete');
  if (marker !== -1) {
    deleteFrom = Number(names[marker + 1]);
    deleteTo = Number(names[marker + 2]);
    names = names.slice(0, marker);
    wanted.clear();
    for (const name of names) wanted.add(name);
  }
  const start = SF.getPositionOfLineAndCharacter(fromLine - 1, 0);
  const endLineText = LINES[toLine - 1] ?? '';
  const end = SF.getPositionOfLineAndCharacter(toLine - 1, endLineText.length);
  const hits = new Map();

  const edits = [];
  const visit = (node) => {
    if (ts.isIdentifier(node) && node.getStart(SF) >= start && node.getEnd() <= end && wanted.has(node.text)) {
      const parent = node.parent;
      const skip =
        (ts.isPropertyAccessExpression(parent) && parent.name === node) ||
        (ts.isPropertyAssignment(parent) && parent.name === node) ||
        (ts.isJsxAttribute(parent) && parent.name === node) ||
        (ts.isBindingElement(parent) && parent.name === node) ||
        (ts.isImportSpecifier(parent) && parent.name === node);
      if (!skip) {
        edits.push({ pos: node.getStart(SF), text: node.text });
        hits.set(node.text, (hits.get(node.text) ?? 0) + 1);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(SF);

  // 从后往前替换，避免偏移漂移。
  edits.sort((a, b) => b.pos - a.pos);
  let out = SOURCE;
  for (const edit of edits) {
    out = `${out.slice(0, edit.pos)}view.${out.slice(edit.pos)}`;
  }
  // `--delete a b`：把 `a..b` 这几行删掉。搬进 hook 的那段逻辑必须真的从 App.tsx 移除，
  // 否则组件里会同时存在 hook 调用与同一份状态（第一版就是这样：构建通过，
  // 但运行时 `view.xxx` 全是 undefined —— 比编译错更坏）。
  let deleted = 0;
  if (deleteFrom !== null && deleteTo !== null) {
    const all = out.split('\n');
    deleted = deleteTo - deleteFrom + 1;
    out = [...all.slice(0, deleteFrom - 1), ...all.slice(deleteTo)].join('\n');
  }
  // **写文件，不走 stdout**：整份 `App.tsx` 有 5 万字符，实测 JSON 走管道会被截断，
  // Python 那侧于是把空字符串当成结果（表现为"找不到 App() 体内第一条语句"）。
  if (outFile) writeFileSync(join(HERE, outFile), out, 'utf8');
  return { count: edits.length, wrote: outFile, chars: out.length, hits: Object.fromEntries(hits), deleted };
}

const [command, ...rest] = CLEAN_ARGS;
let result;
if (command === 'list') result = commandList();
else if (command === 'plan') result = commandPlan(rest);
else if (command === 'identifiers') result = commandIdentifiers(Number(rest[0]), Number(rest[1]));
else if (command === 'rename') result = commandRename(Number(rest[0]), Number(rest[1]), rest[2], ...rest.slice(3));
else {
  console.error(
    '用法：node _ts_extract.mjs list | plan ... | identifiers <from> <to> | rename <from> <to> <Name...>',
  );
  process.exit(2);
}
process.stdout.write(JSON.stringify(result, null, 2));
