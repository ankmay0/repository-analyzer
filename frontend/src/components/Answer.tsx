import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { mermaidSource } from "../mermaid";
import { MermaidDiagram } from "./MermaidDiagram";

/** An answer's markdown, with tables and ```mermaid blocks drawn once the answer is complete. */
export function Answer({ text, pending }: { text: string; pending: boolean }) {
  const components: Components = {
    pre({ node, children, ...props }) {
      const source = mermaidSource(node);
      if (source !== null) return <MermaidDiagram source={source} ready={!pending} />;
      return <pre {...props}>{children}</pre>;
    },
    table({ children }) {
      return (
        <div className="answer-table-wrap">
          <table>{children}</table>
        </div>
      );
    },
  };
  return (
    <Markdown remarkPlugins={[remarkGfm]} components={components}>
      {text}
    </Markdown>
  );
}
