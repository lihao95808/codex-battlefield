import { afterEach, describe, expect, it, vi } from 'vitest';

import { startOneShotProject } from './api';

describe('battlefield API client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
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
