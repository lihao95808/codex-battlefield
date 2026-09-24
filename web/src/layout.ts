import ELK from 'elkjs/lib/elk.bundled.js';
import type { Edge, Node } from '@xyflow/react';

const elk = new ELK();

type ElkPositionedNode = {
  id: string;
  x?: number;
  y?: number;
};

export async function layoutBattlefield(nodes: Node[], edges: Edge[]): Promise<Node[]> {
  const graph = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'DOWN',
      'elk.spacing.nodeNode': '34',
      'elk.layered.spacing.nodeNodeBetweenLayers': '82',
    },
    children: nodes.map((node) => ({
      id: node.id,
      width: node.width ?? 260,
      height: node.height ?? 116,
    })),
    edges: edges.map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  };

  const layouted = await elk.layout(graph);
  const children = (layouted.children ?? []) as ElkPositionedNode[];
  const byId = new Map(children.map((child) => [child.id, child]));
  return nodes.map((node) => {
    const next = byId.get(node.id);
    return {
      ...node,
      position: { x: next?.x ?? 0, y: next?.y ?? 0 },
    };
  });
}
