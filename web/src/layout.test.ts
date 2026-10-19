import { describe, expect, it } from 'vitest';
import type { Edge, Node } from '@xyflow/react';
import { layoutBattlefield } from './layout';
const node = (id: string): Node => ({ id, position: { x: 0, y: 0 }, data: {}, width: 260, height: 116 });
const edge = (source: string, target: string): Edge => ({ id: `${source}-${target}`, source, target });

describe('compact task tree layout', () => {
  it('packs wide sibling groups within three columns and preserves all nodes', async () => {
    const nodes = [node('root'), ...Array.from({ length: 30 }, (_, i) => node(`leaf-${i}`))];
    const result = await layoutBattlefield(nodes, nodes.slice(1).map((child) => edge('root', child.id)));
    expect(result.map(({ id }) => id)).toEqual(nodes.map(({ id }) => id));
    const right = Math.max(...result.map((n) => n.position.x + 260));
    const left = Math.min(...result.map((n) => n.position.x));
    expect(right - left).toBeLessThanOrEqual(930);
    expect(new Set(result.slice(1).map((n) => n.position.y)).size).toBeGreaterThan(1);
    expect(result[1].position.y - result[0].position.y - 116).toBe(164);
    for (const [i, a] of result.entries()) for (const b of result.slice(i + 1)) {
      const overlaps = a.position.x < b.position.x + 260 && a.position.x + 260 > b.position.x
        && a.position.y < b.position.y + 116 && a.position.y + 116 > b.position.y;
      expect(overlaps, `${a.id} overlaps ${b.id}`).toBe(false);
    }
  });
  it('keeps descendants below their parents for nested branch integration', async () => {
    const result = await layoutBattlefield(['root', 'a', 'b', 'aa', 'ab', 'ba'].map(node),
      [edge('root', 'a'), edge('root', 'b'), edge('a', 'aa'), edge('a', 'ab'), edge('b', 'ba')]);
    const pos = new Map(result.map((n) => [n.id, n.position]));
    expect(pos.get('aa')!.y).toBeGreaterThan(pos.get('a')!.y);
    expect(pos.get('ba')!.y).toBeGreaterThan(pos.get('b')!.y);
    expect(pos.get('aa')!.y).not.toBe(pos.get('ab')!.y);
  });
  it('shows malformed cyclic and orphaned components instead of losing them', async () => {
    const result = await layoutBattlefield(['a', 'b', 'orphan'].map(node), [edge('a', 'b'), edge('b', 'a'), edge('missing', 'orphan')]);
    expect(result).toHaveLength(3);
    expect(new Set(result.map((n) => `${n.position.x}:${n.position.y}`)).size).toBe(3);
  });
});
