import { describe, expect, it } from "vitest";

import { type HastNode, mermaidSource } from "./mermaid";

function pre(className: unknown, ...texts: string[]): HastNode {
  return {
    type: "element",
    tagName: "pre",
    children: [
      {
        type: "element",
        tagName: "code",
        properties: { className },
        children: texts.map((value) => ({ type: "text", value })),
      },
    ],
  };
}

describe("mermaidSource", () => {
  it("returns the source of a mermaid block without the trailing newline", () => {
    expect(mermaidSource(pre(["language-mermaid"], "flowchart LR\n", "  a --> b\n"))).toBe(
      "flowchart LR\n  a --> b",
    );
  });

  it("accepts a class string", () => {
    expect(mermaidSource(pre("language-mermaid extra", "graph TD\n a-->b"))).toBe("graph TD\n a-->b");
  });

  it("ignores other code blocks, inline nodes and empty diagrams", () => {
    expect(mermaidSource(pre(["language-python"], "print(1)"))).toBeNull();
    expect(mermaidSource(pre(undefined, "plain"))).toBeNull();
    expect(mermaidSource(pre(["language-mermaid"], "\n  \n"))).toBeNull();
    expect(mermaidSource({ type: "element", tagName: "p", children: [] })).toBeNull();
    expect(mermaidSource(undefined)).toBeNull();
  });
});
