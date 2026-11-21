/**
 * Automatic layout for diagrams.
 *
 * Diagrams arrive as nodes and edges with no positions. Dagre arranges them in
 * layers so arrows mostly point one way, which is the conventional reading for
 * dependency and flow diagrams. This module is pure (no React) so it can be
 * tested on its own.
 */

import dagre from "@dagrejs/dagre";

import type { Diagram } from "./api";

export type Direction = "LR" | "TB";

export interface PlacedNode {
  id: string;
  x: number; // top-left corner
  y: number;
  width: number;
  height: number;
}

export const NODE_HEIGHT = 38;
const MIN_NODE_WIDTH = 90;
const MAX_NODE_WIDTH = 260;
const CHAR_WIDTH = 7.4; // approximate glyph width at the node font size
const NODE_PADDING = 28;

export function nodeWidth(label: string): number {
  const estimated = label.length * CHAR_WIDTH + NODE_PADDING;
  return Math.round(Math.min(MAX_NODE_WIDTH, Math.max(MIN_NODE_WIDTH, estimated)));
}

export function layoutDiagram(diagram: Diagram, direction: Direction): PlacedNode[] {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: direction, nodesep: 28, ranksep: direction === "LR" ? 70 : 54 });
  graph.setDefaultEdgeLabel(() => ({}));

  for (const node of diagram.nodes) {
    graph.setNode(node.id, { width: nodeWidth(node.label), height: NODE_HEIGHT });
  }
  for (const edge of diagram.edges) {
    // Self-loops carry no layout information and dagre handles them poorly.
    if (edge.source !== edge.target) graph.setEdge(edge.source, edge.target);
  }
  dagre.layout(graph);

  return diagram.nodes.map((node) => {
    const placed = graph.node(node.id);
    const width = nodeWidth(node.label);
    // Dagre reports centres; React Flow positions by the top-left corner.
    return {
      id: node.id,
      x: placed.x - width / 2,
      y: placed.y - NODE_HEIGHT / 2,
      width,
      height: NODE_HEIGHT,
    };
  });
}
