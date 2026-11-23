/** Finding and drawing ```mermaid code blocks in chat answers. */

/** The parts of a hast node (react-markdown's syntax tree) that we read. */
export interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

function classNames(node: HastNode): string[] {
  const value = node.properties?.className;
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string");
  return typeof value === "string" ? value.split(/\s+/) : [];
}

function textOf(node: HastNode): string {
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(textOf).join("");
}

/**
 * The diagram source when `pre` is a fenced ```mermaid block, otherwise null.
 * Markdown renders a fenced block as <pre><code class="language-mermaid">.
 */
export function mermaidSource(pre: HastNode | undefined): string | null {
  if (!pre || pre.tagName !== "pre") return null;
  const code = (pre.children ?? []).find((child) => child.type === "element");
  if (!code || code.tagName !== "code" || !classNames(code).includes("language-mermaid")) {
    return null;
  }
  const source = textOf(code).replace(/\n+$/, "");
  return source.trim() ? source : null;
}

type Mermaid = (typeof import("mermaid"))["default"];
let loading: Promise<Mermaid> | null = null;

/** Mermaid is large, so it is only downloaded the first time an answer has a diagram. */
function loadMermaid(): Promise<Mermaid> {
  loading ??= import("mermaid").then(({ default: mermaid }) => {
    const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
    mermaid.initialize({
      startOnLoad: false,
      // Answers quote repository content, so keep Mermaid's sanitising on and
      // click handlers off.
      securityLevel: "strict",
      theme: dark ? "dark" : "default",
    });
    return mermaid;
  });
  return loading;
}

/** Render a diagram to SVG markup. Rejects with Mermaid's message if the syntax is invalid. */
export async function renderMermaid(id: string, source: string): Promise<string> {
  const mermaid = await loadMermaid();
  await mermaid.parse(source);
  const { svg } = await mermaid.render(id, source);
  return svg;
}
