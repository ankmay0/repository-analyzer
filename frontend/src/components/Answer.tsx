import Markdown, { type Components } from "react-markdown";

import { mermaidSource } from "../mermaid";
import { MermaidDiagram } from "./MermaidDiagram";

/** An answer's markdown, with ```mermaid blocks drawn as diagrams once the answer is complete. */
export function Answer({ text, pending }: { text: string; pending: boolean }) {
  const components: Components = {
    pre({ node, children, ...props }) {
      const source = mermaidSource(node);
      if (source !== null) return <MermaidDiagram source={source} ready={!pending} />;
      return <pre {...props}>{children}</pre>;
    },
  };
  return <Markdown components={components}>{text}</Markdown>;
}
