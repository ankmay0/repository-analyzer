/** Pure logic for the Benchmark page: grading badges, live run state and per-approach summaries. */

import type { Progress, ResultRow, RunCurrent, RunEvent, RunSnapshot, RunState } from "./types";

export type Grade = "pass" | "partial" | "fail";

/** pass = every key fact found; partial = some found; fail = none found (or the run errored out). */
export function gradeOf(row: ResultRow): Grade {
  if (row.correct) return "pass";
  if (row.facts_matched > 0) return "partial";
  return "fail";
}

export const GRADE_LABEL: Record<Grade, string> = { pass: "Pass", partial: "Partial", fail: "Fail" };

export function rowKey(approach: string, questionId: string): string {
  return `${approach}\u0000${questionId}`;
}

export interface RunView {
  id: string;
  state: RunState;
  approaches: string[];
  questionIds: string[];
  progress: Progress;
  rows: ResultRow[];
  skipped: Record<string, string>;
  current: RunCurrent | null;
  error: string | null;
}

export function viewFromSnapshot(snapshot: RunSnapshot): RunView {
  return {
    id: snapshot.id,
    state: snapshot.state,
    approaches: snapshot.approaches,
    questionIds: snapshot.question_ids,
    progress: { completed: snapshot.completed, total: snapshot.total },
    rows: snapshot.rows,
    skipped: snapshot.skipped,
    current: snapshot.current,
    error: snapshot.error,
  };
}

export function isFinished(view: RunView | null): boolean {
  return view !== null && view.state !== "running";
}

/** Fold one server event into the view. Events may be replayed, so results are de-duplicated. */
export function applyEvent(view: RunView, event: RunEvent): RunView {
  const next: RunView = { ...view, progress: event.progress };
  switch (event.type) {
    case "plan":
      return { ...next, approaches: event.approaches, questionIds: event.question_ids };
    case "approach_setup":
      return { ...next, current: { approach: event.approach, question_id: null, phase: "setup" } };
    case "approach_started":
      return { ...next, current: { approach: event.approach, question_id: null, phase: "asking" } };
    case "approach_skipped":
      return {
        ...next,
        current: null,
        skipped: { ...view.skipped, [event.approach]: event.reason },
      };
    case "question_started":
      return {
        ...next,
        current: { approach: event.approach, question_id: event.question_id, phase: "asking" },
      };
    case "result": {
      const key = rowKey(event.result.approach, event.result.question_id);
      const rows = view.rows.filter((r) => rowKey(r.approach, r.question_id) !== key);
      return { ...next, rows: [...rows, event.result] };
    }
    case "finished":
      return { ...next, state: "finished", current: null };
    case "failed":
      return { ...next, state: "failed", current: null, error: event.error };
  }
}

export interface ApproachSummary {
  approach: string;
  answered: number;
  pass: number;
  partial: number;
  fail: number;
  /** Share of answers that were fully correct, 0–100. */
  correctPct: number;
  /** Mean share of key facts found, 0–100. */
  factPct: number;
  avgTokens: number;
  avgLatencyMs: number;
}

const mean = (values: number[]): number =>
  values.length === 0 ? 0 : values.reduce((a, b) => a + b, 0) / values.length;

/** One summary per approach that has at least one answer, in the order given. */
export function summarize(rows: ResultRow[], approaches: string[]): ApproachSummary[] {
  return approaches.flatMap((approach) => {
    const mine = rows.filter((r) => r.approach === approach);
    if (mine.length === 0) return [];
    const grades = mine.map(gradeOf);
    const count = (g: Grade) => grades.filter((x) => x === g).length;
    return [
      {
        approach,
        answered: mine.length,
        pass: count("pass"),
        partial: count("partial"),
        fail: count("fail"),
        correctPct: (count("pass") / mine.length) * 100,
        factPct: mean(mine.map((r) => (r.facts_total ? (r.facts_matched / r.facts_total) * 100 : 0))),
        avgTokens: mean(mine.map((r) => r.total_tokens)),
        avgLatencyMs: mean(mine.map((r) => r.latency_ms)),
      },
    ];
  });
}

export function formatTokens(tokens: number): string {
  return tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(Math.round(tokens));
}

export function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatPercent(value: number): string {
  return `${Math.round(value)}%`;
}

/** The question picker's state: everything selected unless the user narrowed it. */
export function toggleId(selected: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(selected);
  if (!next.delete(id)) next.add(id);
  return next;
}
