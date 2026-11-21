import { describe, expect, it } from "vitest";

import type { Diagram } from "./api";
import { layoutDiagram, nodeWidth, type PlacedNode } from "./graphLayout";

function diagram(nodes: string[], edges: [string, string][]): Diagram {
  return {
    title: "t",
    nodes: nodes.map((id) => ({ id, label: id, kind: "repository", status: "normal" })),
    edges: edges.map(([source, target]) => ({ source, target, label: "", dashed: false })),
  };
}

function overlaps(a: PlacedNode, b: PlacedNode): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

describe("layoutDiagram", () => {
  it("places a chain in successive layers along the chosen direction", () => {
    const chain = diagram(["flask", "jinja", "markupsafe"], [["flask", "jinja"], ["jinja", "markupsafe"]]);

    const [a, b, c] = layoutDiagram(chain, "LR");
    expect(a!.x).toBeLessThan(b!.x);
    expect(b!.x).toBeLessThan(c!.x);

    const [top, middle, bottom] = layoutDiagram(chain, "TB");
    expect(top!.y).toBeLessThan(middle!.y);
    expect(middle!.y).toBeLessThan(bottom!.y);
  });

  it("places every node, including unconnected ones, without overlap", () => {
    const placed = layoutDiagram(
      diagram(["a", "b", "c", "d", "alone"], [["a", "b"], ["a", "c"], ["b", "d"], ["c", "d"]]),
      "TB",
    );

    expect(placed.map((node) => node.id)).toEqual(["a", "b", "c", "d", "alone"]);
    for (const node of placed) {
      expect(Number.isFinite(node.x) && Number.isFinite(node.y)).toBe(true);
    }
    for (let i = 0; i < placed.length; i++) {
      for (let j = i + 1; j < placed.length; j++) {
        expect(overlaps(placed[i]!, placed[j]!)).toBe(false);
      }
    }
  });

  it("copes with cycles and self-references", () => {
    const placed = layoutDiagram(diagram(["a", "b"], [["a", "b"], ["b", "a"], ["a", "a"]]), "LR");
    expect(placed).toHaveLength(2);
    expect(overlaps(placed[0]!, placed[1]!)).toBe(false);
  });

  it("sizes nodes to their labels within limits", () => {
    expect(nodeWidth("a")).toBe(90);
    expect(nodeWidth("src/flask/json/provider.py")).toBeGreaterThan(nodeWidth("flask"));
    expect(nodeWidth("x".repeat(200))).toBe(260);
  });
});
