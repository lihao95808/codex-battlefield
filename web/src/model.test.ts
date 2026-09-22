import { describe, expect, it } from 'vitest';

import {
  constitutionBoardFromStats,
  constitutionPropagationHeadline,
  formatObligationAge,
  isActiveFrontier,
  obligationBoardFromStats,
  obligationBoardHeadline,
  obligationTypeBreakdown,
  outcomePreviewForTask,
  summarizeStats,
  versionDistributionEntries,
} from './model';
import type { BattlefieldTask, ConstitutionBoard, ObligationBoard } from './types';

function task(id: string, parent_id: string | null, status: BattlefieldTask['status'], depth: number): BattlefieldTask {
  return {
    id,
    parent_id,
    title: id,
    description: '',
    depth,
    status,
    scope: [],
    acceptance: [],
    node_meta: {},
    constitution_id: null,
    constitution_hash: null,
    constitution_version: null,
    leaf_attempts: 0,
    verification_failures: 0,
    converted_from_leaf: false,
    progress_current: 0,
    progress_total: 0,
    result: null,
    error_message: null,
    branch: null,
    worktree_path: null,
    is_active_frontier: false,
    created_at: '',
    updated_at: '',
  };
}

describe('battlefield model helpers', () => {
  it('marks only non-terminal leaves as active frontier', () => {
    const root = task('root', null, 'running', 0);
    const runningLeaf = task('leaf-a', 'root', 'running', 1);
    const doneLeaf = task('leaf-b', 'root', 'done', 1);
    const parentIds = new Set(['root']);

    expect(isActiveFrontier(root, parentIds)).toBe(false);
    expect(isActiveFrontier(runningLeaf, parentIds)).toBe(true);
    expect(isActiveFrontier(doneLeaf, parentIds)).toBe(false);
  });

  it('summarizes active frontier and depth', () => {
    const stats = summarizeStats([
      task('root', null, 'running', 0),
      task('leaf-a', 'root', 'running', 1),
      task('leaf-b', 'root', 'blocked', 1),
    ]);

    expect(stats).toEqual({ total: 3, active_frontier: 1, max_depth: 1 });
  });

  it('reads the obligation board out of stats and headlines it honestly', () => {
    const board: ObligationBoard = {
      blocking_total: 2,
      advisory_total: 1,
      unsettled_total: 3,
      by_type: { integration_conflict: 1, revalidate_required: 1, integration_skipped: 1 },
      by_depth: { 1: 2 },
      oldest_blocking_age_seconds: 7200,
      revalidate_queue_depth: 1,
      settled_by_human_count: 0,
      settled_total: 4,
      recurrence_total: 0,
    };

    expect(obligationBoardFromStats({ obligations: board })).toEqual(board);
    expect(obligationBoardFromStats(undefined)).toBeNull();
    expect(obligationBoardFromStats({ total: 3 })).toBeNull();
    expect(obligationBoardHeadline(board)).toBe('阻断 2 · 提示 1 · 最久 2 小时');
    expect(obligationTypeBreakdown(board)).toEqual([
      { type: 'integration_conflict', label: '集成冲突', count: 1 },
      { type: 'integration_skipped', label: '集成跳过', count: 1 },
      { type: 'revalidate_required', label: '待再验证', count: 1 },
    ]);
  });

  it('never dresses an unjudgeable age up as zero seconds', () => {
    // "判不出卡了多久"与"刚记上"是两件事，界面上不能混成一句
    const board: ObligationBoard = {
      blocking_total: 1,
      advisory_total: 0,
      unsettled_total: 1,
      by_type: { constitution_stale: 1 },
      by_depth: {},
      oldest_blocking_age_seconds: null,
      revalidate_queue_depth: 0,
      settled_by_human_count: 0,
      settled_total: 0,
      recurrence_total: 0,
    };

    expect(obligationBoardHeadline(board)).toBe('阻断 1 · 卡了多久：判不出');
    expect(
      obligationBoardHeadline({ ...board, blocking_total: 0, advisory_total: 0, by_type: {} }),
    ).toBe('无未清债务');
    expect(formatObligationAge(45)).toBe('45 秒');
    expect(formatObligationAge(90 * 60)).toBe('1 小时');
    expect(formatObligationAge(3 * 86400)).toBe('3 天');
  });

  it('separates "must wait for the running ones" from "left behind"', () => {
    // 混成一句会让人往两个相反方向误判：以为树卡了，或以为它在干活
    const board: ConstitutionBoard = {
      effective_version: 2,
      effective_hash: 'hash-v2',
      node_total: 10,
      version_distribution: { '2': 7, '1': 2, unknown: 1 },
      stale_total: 2,
      stale_by_action: { wait_running: ['a'], revalidate: ['b'] },
      running_on_old_version: 1,
      revalidate_queue_depth: 1,
      last_amendment: { from_version: 1, to_version: 2, direction: 'tighten', actor: 'operator' },
    };

    expect(constitutionBoardFromStats({ constitution: board })).toEqual(board);
    expect(constitutionBoardFromStats({})).toBeNull();
    expect(constitutionPropagationHeadline(board)).toBe('2 个节点落后 · 其中 1 个执行中必须等 · 待再验证 1 · 3 个版本并存');
    expect(versionDistributionEntries(board)).toEqual([
      { version: '1', count: 2 },
      { version: '2', count: 7 },
      { version: 'unknown', count: 1 },
    ]);
  });

  it('says "not bound" instead of showing v0 when there is no constitution', () => {
    const board: ConstitutionBoard = {
      effective_version: null,
      effective_hash: null,
      node_total: 0,
      version_distribution: {},
      stale_total: 0,
      stale_by_action: {},
      running_on_old_version: 0,
      revalidate_queue_depth: 0,
      last_amendment: null,
    };
    expect(constitutionPropagationHeadline(board)).toBe('未绑定宪法');
    expect(constitutionPropagationHeadline({ ...board, effective_version: 3 })).toBe('全部节点在 v3');
  });

  it('shows an image preview for successful nodes with image evidence', () => {
    const done = task('shot', null, 'done', 4);
    done.worktree_path = 'C:\\project\\workspace\\worktrees\\shot';
    done.result = {
      worker: {
        status: 'succeeded',
        changed_files: ['assets/ui/result-preview.png'],
      },
    };

    const preview = outcomePreviewForTask(done);

    expect(preview?.kind).toBe('image');
    expect(preview?.title).toContain('result-preview.png');
    expect(preview && preview.kind === 'image' ? preview.imageUrl : '').toContain('/api/files/preview?path=');
  });

  it('shows artifact badges only for high-value successful outcomes', () => {
    const finalGate = task('final', null, 'done', 4);
    finalGate.title = 'Final product gate repair';
    finalGate.result = {
      worker: {
        status: 'succeeded',
        changed_files: ['README.md'],
        evidence: ['acceptance passed'],
      },
    };
    const ordinaryLeaf = task('leaf', null, 'done', 5);
    ordinaryLeaf.result = {
      worker: {
        status: 'succeeded',
        changed_files: ['README.md'],
      },
    };

    expect(outcomePreviewForTask(finalGate)?.kind).toBe('artifact');
    expect(outcomePreviewForTask(ordinaryLeaf)).toBeNull();
  });
});
