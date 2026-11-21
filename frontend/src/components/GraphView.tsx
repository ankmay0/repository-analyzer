import {
  Background,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { useMemo } from "react";

import "@xyflow/react/style.css";

import type { Diagram } from "../api";
import { layoutDiagram, type Direction } from "../graphLayout";

interface Props {
  diagram: Diagram;
  /** "LR" reads left to right and suits wide areas; "TB" suits narrow ones. */
  direction?: Direction;
  /** Fixed height in pixels. By default the height follows the size of the diagram. */
  height?: number;
  onNodeClick?: (id: string) => void;
}

/**
 * An interactive diagram: pan, zoom and drag nodes. Positions come from an
 * automatic layout; the viewer can rearrange them but cannot edit the graph.
 */
const MIN_HEIGHT = 180;
const MAX_HEIGHT = 460;
const VERTICAL_MARGIN = 110; // room for padding and the controls

export function GraphView({ diagram, direction = "LR", height, onNodeClick }: Props) {
  const { nodes, edges, naturalHeight } = useMemo(() => {
    const layout = layoutDiagram(diagram, direction);
    const placed = new Map(layout.map((node) => [node.id, node]));
    const top = Math.min(...layout.map((node) => node.y));
    const bottom = Math.max(...layout.map((node) => node.y + node.height));
    const naturalHeight = Math.min(
      MAX_HEIGHT,
      Math.max(MIN_HEIGHT, (layout.length ? bottom - top : 0) + VERTICAL_MARGIN),
    );
    const horizontal = direction === "LR";

    const flowNodes: Node[] = diagram.nodes.map((node) => {
      const position = placed.get(node.id);
      return {
        id: node.id,
        position: { x: position?.x ?? 0, y: position?.y ?? 0 },
        data: { label: node.label },
        className: `flow-node flow-kind-${node.kind} flow-status-${node.status}`,
        style: { width: position?.width },
        sourcePosition: horizontal ? Position.Right : Position.Bottom,
        targetPosition: horizontal ? Position.Left : Position.Top,
      };
    });

    const flowEdges: Edge[] = diagram.edges.map((edge, index) => ({
      id: `${index}:${edge.source}->${edge.target}`,
      source: edge.source,
      target: edge.target,
      label: edge.label || undefined,
      markerEnd: { type: MarkerType.ArrowClosed },
      className: edge.dashed ? "flow-edge-dashed" : undefined,
    }));

    return { nodes: flowNodes, edges: flowEdges, naturalHeight };
  }, [diagram, direction]);

  return (
    <div className="flow" style={{ height: height ?? naturalHeight }} role="img" aria-label={diagram.title}>
      {/* Uncontrolled, so dragging works without state here; the key re-mounts the
          flow when the diagram itself changes. */}
      <ReactFlow
        key={JSON.stringify(diagram)}
        defaultNodes={nodes}
        defaultEdges={edges}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 1.2 }}
        minZoom={0.2}
        nodesConnectable={false}
        edgesFocusable={false}
        deleteKeyCode={null}
        proOptions={{ hideAttribution: false }}
        onNodeClick={onNodeClick ? (_, node) => onNodeClick(node.id) : undefined}
      >
        <Background gap={18} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
