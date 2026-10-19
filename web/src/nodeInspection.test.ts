import { describe, expect, it } from 'vitest';
import { readableRunLog, completionReport, taskImageArtifacts } from './nodeInspection';
import type { BattlefieldTask } from './types';

describe('node evidence presentation', () => {
  it('shows only recorded model messages and tool output, with raw fallback', () => {
    const rows = readableRunLog('{"type":"item.completed","item":{"type":"agent_message","text":"Done"}}\n{"item":{"type":"command_execution","command":"test","aggregated_output":"ok"}}\nplain log');
    expect(rows[0]).toMatchObject({ label: '模型回复', text: 'Done' });
    expect(rows[1].text).toContain('ok');
    expect(rows[2].text).toBe('plain log');
  });
  it('bounds logs to recent 300 lines', () => {
    const rows = readableRunLog(Array.from({ length: 400 }, (_, i) => `line${i}`).join('\n'));
    expect(rows).toHaveLength(300); expect(rows[0].text).toBe('line100');
  });
  it('resolves actual reported image paths without allowing traversal or remote URLs', () => {
    const task = { worktree_path: 'D:/work/node', result: { worker: { changed_files: ['assets/a.png', 'assets/a.png', '../secret.png', 'https://remote/a.png', 'README.md'] } }, node_meta: {} } as unknown as BattlefieldTask;
    const images = taskImageArtifacts(task);
    expect(images).toHaveLength(1); expect(images[0].path).toBe('D:/work/node/assets/a.png');
    expect(images[0].url).toContain('/api/files/preview?path=');
  });
  it('takes the finished-node summary from real records only', () => {
    const task = { result: { worker: { summary: '导出了三张界面截图', evidence: ['a', 'a'], changed_files: ['ui/1.png'] } } } as unknown as BattlefieldTask;
    const report = completionReport(task, [{ result: { acceptance: { summary: '验收通过' } } }]);
    expect(report.summary).toBe('导出了三张界面截图');
    expect(report.evidence).toEqual(['a']);
    expect(report.files).toEqual(['ui/1.png']);
  });
  it('never invents a completion summary when records are empty', () => {
    const report = completionReport({ result: null } as unknown as BattlefieldTask, [{ result: null }]);
    expect(report).toEqual({ summary: null, evidence: [], files: [] });
  });
});
