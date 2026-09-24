import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchObligationIndex, startOneShotProject } from './api';

describe('battlefield API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('asks the obligation index for an explicit status scope', async () => {
    const board = {
      status: 'open',
      total: 0,
      by_type: {},
      blocking_by_type: {},
      by_root: {},
      built_at: null,
      in_sync: false,
      drift_counts: { missing: 3, extra: 0, changed: 0 },
      rebuild_command: 'python scripts/rebuild_obligation_index.py --rebuild',
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => board,
    }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await fetchObligationIndex();
    expect(result).toEqual(board);
    expect(fetchMock).toHaveBeenCalledWith('/api/obligation-index?status=open');

    await fetchObligationIndex('settled');
    expect(fetchMock).toHaveBeenLastCalledWith('/api/obligation-index?status=settled');
  });

  it('sends only the goal to the one-shot recursive project endpoint', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ root_id: 'root-test', status: 'started' }),
    }));
    vi.stubGlobal('fetch', fetchMock);

    const goal = '开发一个2D 街机 格斗 闯关 游戏 godot 品质和市面上街机一个水平';
    const result = await startOneShotProject(goal);

    expect(result).toEqual({ root_id: 'root-test', status: 'started' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith('/api/projects/one-shot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal }),
    });
    const init = fetchMock.mock.calls[0]?.[1];
    expect(init).toBeDefined();
    expect(Object.keys(JSON.parse(String(init?.body)))).toEqual(['goal']);
  });
});
