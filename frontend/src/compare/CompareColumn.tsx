import Markdown from "react-markdown";

import { formatLatency } from "../usage";
import type { ColumnView } from "./model";

interface Props {
  column: ColumnView;
  fewestTokens: boolean;
  fastest: boolean;
}

const NUMBER = new Intl.NumberFormat("en-US");

const STATE_LABEL: Record<ColumnView["state"], string> = {
  pending: "Waiting",
  preparing: "Preparing",
  streaming: "Answering",
  done: "Done",
  error: "Error",
  unavailable: "Unavailable",
};

export function CompareColumn({ column, fewestTokens, fastest }: Props) {
  const { usage } = column;
  const unavailable = column.state === "unavailable";
  const busy = column.state === "preparing" || column.state === "streaming";
  return (
    <section
      className={`compare-column compare-${column.state}`}
      data-approach={column.name}
      aria-label={`${column.name} answer`}
      aria-disabled={unavailable}
      aria-busy={busy}
    >
      <header>
        <h3>
          <span className="approach-swatch" aria-hidden="true" /> {column.name}
        </h3>
        <span className="compare-state">{STATE_LABEL[column.state]}</span>
      </header>
      <p className="muted compare-summary">{column.summary}</p>

      <div className="compare-body answer">
        {unavailable && <p className="approach-reason">Unavailable: {column.reason}</p>}
        {column.state === "preparing" && <p className="muted spinner">{column.setupMessage ?? "Preparing"}…</p>}
        {column.state === "pending" && <p className="muted">Waiting for the answer…</p>}
        {column.text && <Markdown>{column.text}</Markdown>}
        {column.state === "streaming" && !column.text && <p className="muted spinner">Thinking…</p>}
        {column.error && (
          <p className="notice notice-error" role="alert">
            {column.error}
          </p>
        )}
      </div>

      {column.toolCalls.length > 0 && (
        <details className="steps">
          <summary>
            {column.toolCalls.length} tool {column.toolCalls.length === 1 ? "call" : "calls"}
          </summary>
          <ol>
            {column.toolCalls.map((name, index) => (
              <li key={index}>{name}</li>
            ))}
          </ol>
        </details>
      )}

      {!unavailable && (
        <footer className="compare-footer">
          {usage ? (
            <dl>
              <div className={fewestTokens ? "compare-best" : undefined}>
                <dt>Tokens</dt>
                <dd>
                  {NUMBER.format(usage.total_tokens)}
                  {fewestTokens && <span className="compare-badge">Fewest tokens</span>}
                </dd>
                <dd className="muted compare-detail">
                  {NUMBER.format(usage.input_tokens)} in · {NUMBER.format(usage.output_tokens)} out
                  {usage.cache_read_tokens > 0 && ` · ${NUMBER.format(usage.cache_read_tokens)} cached`}
                </dd>
              </div>
              <div className={fastest ? "compare-best" : undefined}>
                <dt>Latency</dt>
                <dd>
                  {formatLatency(usage.latency_ms)}
                  {fastest && <span className="compare-badge">Fastest</span>}
                </dd>
                <dd className="muted compare-detail">
                  {usage.tool_calls} tool {usage.tool_calls === 1 ? "call" : "calls"} · {usage.llm_calls} model{" "}
                  {usage.llm_calls === 1 ? "call" : "calls"}
                </dd>
              </div>
            </dl>
          ) : (
            <p className="muted">{column.state === "error" ? "No usage reported" : "Tokens and latency appear when the answer is done"}</p>
          )}
          {column.setup && column.setup.total_tokens > 0 && (
            <p className="muted compare-setup">
              + {NUMBER.format(column.setup.total_tokens)} one-off setup tokens ({column.setup.description})
            </p>
          )}
        </footer>
      )}
    </section>
  );
}
