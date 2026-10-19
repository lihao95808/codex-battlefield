import type { BattlefieldTask } from './types';

export interface LogEntry { id: string; label: string; text: string }
const LABELS: Record<string, string> = {
  agent_message: '模型回复', reasoning: '已公开的推理摘要', command_execution: '命令执行',
  tool_call: '工具调用', tool_result: '工具结果', file_change: '文件修改', error: '错误',
};

// Only use text actually recorded by the runner. Never infer private reasoning.
export function readableRunLog(content: string): LogEntry[] {
  return content.split('\n').filter((line) => line.trim()).slice(-300).map((line, index) => {
    try {
      const event = JSON.parse(line) as Record<string, unknown>;
      const item = event.item && typeof event.item === 'object' ? event.item as Record<string, unknown> : event;
      const kind = String(item.type ?? event.type ?? '记录');
      const parts = [item.text, item.message, item.command, item.aggregated_output, item.output]
        .filter((value): value is string => typeof value === 'string' && value.length > 0);
      return { id: String(index), label: LABELS[kind] ?? kind, text: parts.join('\n\n') || JSON.stringify(event, null, 2) };
    } catch { return { id: String(index), label: '运行日志', text: line }; }
  });
}

export interface CompletionReport {
  summary: string | null;
  evidence: string[];
  files: string[];
}

function stringsOf(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string' && item.trim() !== '');
}

/**
 * 已完成节点只报“做了什么”，不回放过程。
 *
 * 摘要/证据/文件全部**取自实际记录**（task.result 与各轮 run.result）；
 * 一条都没有时 summary 返回 null —— 界面必须说“没有可读汇报”，
 * 不能拿 done 状态反推它完成了什么。
 */
export function completionReport(
  task: BattlefieldTask,
  runs: { result: Record<string, unknown> | null }[] = [],
): CompletionReport {
  const payloads: Record<string, unknown>[] = [];
  const push = (value: unknown) => {
    if (value && typeof value === 'object' && !Array.isArray(value)) payloads.push(value as Record<string, unknown>);
  };
  push(task.result);
  for (const key of ['worker', 'acceptance', 'verifier']) push((task.result ?? {})[key]);
  for (const run of [...runs].reverse()) push(run.result);
  const summary = payloads
    .map((payload) => payload.summary)
    .find((value): value is string => typeof value === 'string' && value.trim() !== '') ?? null;
  const evidence = [...new Set(payloads.flatMap((payload) => stringsOf(payload.evidence)))].slice(0, 50);
  const files = [...new Set(payloads.flatMap((payload) => stringsOf(payload.changed_files)))].slice(0, 100);
  return { summary: summary === null ? null : summary.trim(), evidence, files };
}

export function taskImageArtifacts(task: BattlefieldTask): { path: string; name: string; url: string }[] {
  const paths = new Set<string>();
  const visit = (value: unknown, depth: number) => {
    if (depth > 8 || paths.size >= 24) return;
    if (typeof value === 'string') {
      const raw = value.trim().replace(/^`|`$/g, '');
      if (!/\.(png|jpe?g|webp|gif|bmp)$/i.test(raw) || /^https?:/i.test(raw) || raw.includes('\n')) return;
      const clean = raw.replace(/^res:\/\//, '').replace(/^\.\//, '');
      if (clean.split(/[\\/]/).includes('..')) return;
      const absolute = /^(?:[A-Za-z]:[\\/]|\/)/.test(clean);
      if (!absolute && !task.worktree_path) return;
      paths.add(absolute ? clean : `${task.worktree_path!.replace(/[\\/]$/, '')}/${clean}`);
    } else if (Array.isArray(value)) value.forEach((v) => visit(v, depth + 1));
    else if (value && typeof value === 'object') Object.values(value).forEach((v) => visit(v, depth + 1));
  };
  visit(task.result, 0);
  visit(task.node_meta.outcome_preview, 0);
  visit(task.node_meta.artifact_preview, 0);
  return [...paths].map((path) => ({ path, name: path.split(/[\\/]/).pop() ?? path, url: `/api/files/preview?path=${encodeURIComponent(path)}` }));
}
