import { useEffect, useRef } from "react";
import Markdown from "react-markdown";

import { formatSeconds, GRADE_LABEL, gradeOf } from "./model";
import type { BenchmarkQuestion, ResultRow } from "./types";

interface Props {
  row: ResultRow;
  question: BenchmarkQuestion;
  onClose: () => void;
}

export function CellDetail({ row, question, onClose }: Props) {
  const grade = gradeOf(row);
  const matched = new Set(row.matched_facts);
  const ref = useRef<HTMLElement>(null);
  // The panel sits below the grid; bring it into view when another cell is picked.
  useEffect(() => {
    ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [row.approach, row.question_id]);
  return (
    <section ref={ref} className="cell-detail" aria-label={`Answer from ${row.approach} to ${question.id}`}>
      <header>
        <h3>
          {question.id} · {row.approach} <span className={`badge badge-${grade}`}>{GRADE_LABEL[grade]}</span>
        </h3>
        <button type="button" className="secondary" onClick={onClose}>
          Close
        </button>
      </header>
      <p className="detail-question">{question.question}</p>
      <p className="muted detail-metrics">
        {row.input_tokens.toLocaleString("en-US")} in / {row.output_tokens.toLocaleString("en-US")} out
        ({row.cache_read_tokens.toLocaleString("en-US")} cache read) · {row.llm_calls} model calls ·{" "}
        {row.tool_calls} tool calls{row.tools_used.length > 0 ? ` (${[...new Set(row.tools_used)].join(", ")})` : ""} ·{" "}
        {formatSeconds(row.latency_ms)}
      </p>
      {row.error && (
        <p className="notice notice-error" role="alert">
          {row.error}
        </p>
      )}
      <h4>Key facts ({row.facts_matched} of {row.facts_total} found)</h4>
      <ul className="facts">
        {question.key_facts.map((fact) => (
          <li key={fact} className={matched.has(fact) ? "fact-matched" : "fact-missed"}>
            <span aria-hidden="true">{matched.has(fact) ? "✓" : "✗"}</span>
            <span className="visually-hidden">{matched.has(fact) ? "Matched: " : "Missed: "}</span>
            <code>{fact}</code>
          </li>
        ))}
      </ul>
      {row.judge_correct !== null && (
        <p className="detail-judge">
          LLM judge: <strong>{row.judge_correct ? "correct" : "incorrect"}</strong>
          {row.judge_reason ? ` — ${row.judge_reason}` : ""}
        </p>
      )}
      <h4>Answer</h4>
      <div className="answer">{row.answer ? <Markdown>{row.answer}</Markdown> : <p className="muted">(no answer)</p>}</div>
      <details>
        <summary>Reference answer</summary>
        <div className="answer">
          <Markdown>{question.expected_answer}</Markdown>
        </div>
      </details>
    </section>
  );
}
