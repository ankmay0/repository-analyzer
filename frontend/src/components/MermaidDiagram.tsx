import { useEffect, useId, useState } from "react";

import { renderMermaid } from "../mermaid";

type State = { svg: string; error: null } | { svg: null; error: string | null };

/**
 * A ```mermaid block drawn as a diagram. Until the answer has finished
 * streaming (or if the diagram is invalid) the source is shown as code.
 */
export function MermaidDiagram({ source, ready }: { source: string; ready: boolean }) {
  const id = `mermaid-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const [state, setState] = useState<State>({ svg: null, error: null });

  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    renderMermaid(id, source).then(
      (svg) => {
        if (!cancelled) setState({ svg, error: null });
      },
      (cause: unknown) => {
        if (cancelled) return;
        const message = cause instanceof Error ? cause.message : String(cause);
        const firstLine = (message.split("\n")[0] ?? message).replace(/:$/, "");
        setState({ svg: null, error: firstLine });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [id, source, ready]);

  if (state.svg !== null) {
    return (
      <figure className="mermaid-diagram">
        {/* Mermaid output in strict mode is sanitised with DOMPurify. */}
        <div className="mermaid-svg" dangerouslySetInnerHTML={{ __html: state.svg }} />
        <details className="mermaid-source">
          <summary>Diagram source</summary>
          <pre>
            <code className="language-mermaid">{source}</code>
          </pre>
        </details>
      </figure>
    );
  }

  return (
    <figure className="mermaid-diagram">
      {state.error !== null && (
        <figcaption className="muted">Could not draw this diagram ({state.error}). Showing its source.</figcaption>
      )}
      <pre>
        <code className="language-mermaid">{source}</code>
      </pre>
    </figure>
  );
}
