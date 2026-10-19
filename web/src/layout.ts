import type { Edge, Node } from '@xyflow/react';

// Bound the canvas width instead of putting every sibling on one ELK layer.
// Logical depth/parent edges stay unchanged: only visual positions are packed.
// Cycles or missing parents fail toward visibility (shown as separate roots),
// never silently dropping nodes or changing task relationships.
const NODE_WIDTH = 260;
const NODE_HEIGHT = 116;
const COLUMN_GAP = 34;
const LEVEL_GAP = 164; // formerly 82: twice the vertical parent/child spacing
const ROW_GAP = 34;
const INDENT = 20;
const MAX_COLUMNS = 3;
const MAX_WIDTH = NODE_WIDTH * MAX_COLUMNS + COLUMN_GAP * (MAX_COLUMNS - 1) + INDENT * 4;

export async function layoutBattlefield(nodes: Node[], edges: Edge[]): Promise<Node[]> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const children = new Map<string, string[]>();
  const parent = new Map<string, string>();
  for (const edge of edges) {
    if (!byId.has(edge.source) || !byId.has(edge.target) || edge.source === edge.target) continue;
    // One task has one parent; duplicate edges must not duplicate placement.
    if (parent.has(edge.target)) continue;
    parent.set(edge.target, edge.source);
    children.set(edge.source, [...(children.get(edge.source) ?? []), edge.target]);
  }
  const placed = new Map<string, { x: number; y: number }>();
  const widthOf = (id: string) => byId.get(id)?.width ?? NODE_WIDTH;
  const heightOf = (id: string) => byId.get(id)?.height ?? NODE_HEIGHT;

  const placeBranch = (id: string, x: number, y: number, width: number): number => {
    if (placed.has(id)) return y;
    placed.set(id, { x: x + Math.max(0, (width - widthOf(id)) / 2), y });
    let bottom = y + heightOf(id);
    const descendants = (children.get(id) ?? []).filter((child) => !placed.has(child));
    if (!descendants.length) return bottom;
    const inset = width >= NODE_WIDTH + INDENT * 2 ? INDENT : 0;
    const available = width - inset * 2;
    const minWidth = Math.max(NODE_WIDTH, ...descendants.map(widthOf));
    const columns = Math.max(1, Math.min(MAX_COLUMNS, descendants.length,
      Math.floor((available + COLUMN_GAP) / (minWidth + COLUMN_GAP))));
    const laneWidth = (available - COLUMN_GAP * (columns - 1)) / columns;
    let rowY = bottom + LEVEL_GAP;
    for (let offset = 0; offset < descendants.length; offset += columns) {
      let rowBottom = rowY;
      descendants.slice(offset, offset + columns).forEach((child, column) => {
        rowBottom = Math.max(rowBottom, placeBranch(child, x + inset + column * (laneWidth + COLUMN_GAP), rowY, laneWidth));
      });
      bottom = rowBottom;
      rowY = rowBottom + ROW_GAP;
    }
    return bottom;
  };

  let bottom = 0;
  const roots = nodes.filter((node) => !parent.has(node.id));
  for (const root of roots) {
    bottom = placeBranch(root.id, 0, bottom, Math.max(MAX_WIDTH, widthOf(root.id))) + LEVEL_GAP;
  }
  // Preserve visibility for malformed cyclic components without infinite recursion.
  for (const node of nodes) {
    if (!placed.has(node.id)) bottom = placeBranch(node.id, 0, bottom, Math.max(MAX_WIDTH, widthOf(node.id))) + LEVEL_GAP;
  }
  return nodes.map((node) => ({ ...node, position: placed.get(node.id) ?? { x: 0, y: 0 } }));
}
