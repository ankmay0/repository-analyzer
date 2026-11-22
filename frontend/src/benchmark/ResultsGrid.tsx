import { formatSeconds, formatTokens, GRADE_LABEL, gradeOf, rowKey } from "./model";
import type { BenchmarkQuestion, ResultRow, RunCurrent } from "./types";

interface Props {
  questions: BenchmarkQuestion[];
  approaches: string[];
  rows: ResultRow[];
  skipped: Record<string, string>;
  current: RunCurrent | null;
  selected: { approach: string; questionId: string } | null;
  onSelect: (approach: string, questionId: string) => void;
}

function Cell({
  row,
  active,
  onSelect,
}: {
  row: ResultRow;
  active: boolean;
  onSelect: () => void;
}) {
  const grade = gradeOf(row);
  return (
    <button
      type="button"
      className={`cell cell-${grade}${active ? " cell-selected" : ""}`}
      aria-pressed={active}
      aria-label={`${row.approach}, ${row.question_id}: ${GRADE_LABEL[grade]}`}
      onClick={onSelect}
    >
      <span className={`badge badge-${grade}`}>{GRADE_LABEL[grade]}</span>
      <span className="cell-facts">
        {row.facts_matched}/{row.facts_total} facts
      </span>
      <span className="cell-metrics">
        {formatTokens(row.total_tokens)} tok · {formatSeconds(row.latency_ms)}
      </span>
    </button>
  );
}

export function ResultsGrid({ questions, approaches, rows, skipped, current, selected, onSelect }: Props) {
  const byKey = new Map(rows.map((row) => [rowKey(row.approach, row.question_id), row]));
  return (
    <div className="grid-scroll">
      <table className="results-grid">
        <thead>
          <tr>
            <th scope="col">Question</th>
            {approaches.map((approach) => (
              <th key={approach} scope="col" data-approach={approach}>
                <span className="approach-swatch" aria-hidden="true" /> {approach}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {questions.map((question) => (
            <tr key={question.id}>
              <th scope="row" title={question.question}>
                <span className="question-id">{question.id}</span>
                <span className="muted question-category">{question.category}</span>
              </th>
              {approaches.map((approach) => {
                const row = byKey.get(rowKey(approach, question.id));
                if (row) {
                  return (
                    <td key={approach}>
                      <Cell
                        row={row}
                        active={selected?.approach === approach && selected.questionId === question.id}
                        onSelect={() => onSelect(approach, question.id)}
                      />
                    </td>
                  );
                }
                const reason = skipped[approach];
                const running = current?.approach === approach && current.question_id === question.id;
                return (
                  <td key={approach} className="cell-empty" title={reason}>
                    {reason !== undefined ? "skipped" : running ? <span className="spinner">running…</span> : "·"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
