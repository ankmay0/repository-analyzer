/** Pure logic for the Compare page: per-approach columns, highlights and session history. */

import type { Usage } from "../usage";
import type { CompareApproach, CompareEvent, SetupCost } from "./types";

export type ColumnState = "pending" | "preparing" | "streaming" | "done" | "error" | "unavailable";

export interface ColumnView {
  name: string;
  summary: string;
  state: ColumnState;
  /** Why the approach cannot run (state "unavailable"). */
  reason: string | null;
  /** What the approach is preparing (state "preparing"). */
  setupMessage: string | null;
  text: string;
  toolCalls: string[];
  usage: Usage | null;
  setup: SetupCost | null;
  error: string | null;
}

export interface CompareRun {
  id: number;
  question: string;
  repository: string;
  label: string;
  columns: ColumnView[];
  finished: boolean;
  /** A problem with the whole request (for example no API key). */
  error: string | null;
}

export function emptyColumn(approach: CompareApproach): ColumnView {
  return {
    name: approach.name,
    summary: approach.summary,
    state: approach.available ? "pending" : "unavailable",
    reason: approach.available ? null : approach.reason,
    setupMessage: null,
    text: "",
    toolCalls: [],
    usage: null,
    setup: null,
    error: null,
  };
}

export function newRun(id: number, question: string, repository: string, label: string): CompareRun {
  return { id, question, repository, label, columns: [], finished: false, error: null };
}

function updateColumn(
  run: CompareRun,
  name: string,
  change: (column: ColumnView) => ColumnView,
): CompareRun {
  return { ...run, columns: run.columns.map((c) => (c.name === name ? change(c) : c)) };
}

/** Fold one server event into the run. Unknown approaches and event types are ignored. */
export function applyCompareEvent(run: CompareRun, event: CompareEvent): CompareRun {
  switch (event.type) {
    case "plan":
      return { ...run, label: event.label, columns: event.approaches.map(emptyColumn) };
    case "setup":
      return updateColumn(run, event.approach, (c) => ({ ...c, state: "preparing", setupMessage: event.message }));
    case "started":
      return updateColumn(run, event.approach, (c) => ({ ...c, state: "streaming", setupMessage: null }));
    case "token":
      return updateColumn(run, event.approach, (c) => ({ ...c, state: "streaming", text: c.text + event.text }));
    case "tool_call":
      return updateColumn(run, event.approach, (c) => ({ ...c, toolCalls: [...c.toolCalls, event.name] }));
    case "error":
      return updateColumn(run, event.approach, (c) => ({ ...c, state: "error", error: event.message }));
    case "unavailable":
      return updateColumn(run, event.approach, (c) => ({ ...c, state: "unavailable", reason: event.reason }));
    case "usage":
      return updateColumn(run, event.approach, (c) => ({
        ...c,
        usage: event.usage,
        setup: event.setup,
        state: c.state === "error" ? "error" : "done",
      }));
    case "approach_done":
      return updateColumn(run, event.approach, (c) =>
        c.state === "pending" || c.state === "preparing" || c.state === "streaming"
          ? { ...c, state: c.error ? "error" : "done", setupMessage: null }
          : c,
      );
    case "done":
      return { ...run, finished: true };
  }
}

/** Mark a run that ended early (aborted or failed) as finished, closing any open columns. */
export function finishRun(run: CompareRun, error: string | null = null): CompareRun {
  return {
    ...run,
    finished: true,
    error: error ?? run.error,
    columns: run.columns.map((c) =>
      c.state === "pending" || c.state === "preparing" || c.state === "streaming"
        ? { ...c, state: error ? "error" : "done", error: c.error ?? error, setupMessage: null }
        : c,
    ),
  };
}

export interface Highlights {
  fewestTokens: ReadonlySet<string>;
  fastest: ReadonlySet<string>;
}

/**
 * Which approaches used the fewest tokens and answered fastest. Only columns that finished
 * with a usage report count, and nothing is highlighted until at least two have one.
 */
export function highlights(columns: ColumnView[]): Highlights {
  const measured = columns.filter((c) => c.usage !== null && c.state === "done");
  if (measured.length < 2) return { fewestTokens: new Set(), fastest: new Set() };
  const pick = (value: (usage: Usage) => number): Set<string> => {
    const best = Math.min(...measured.map((c) => value(c.usage as Usage)));
    return new Set(measured.filter((c) => value(c.usage as Usage) === best).map((c) => c.name));
  };
  return { fewestTokens: pick((u) => u.total_tokens), fastest: pick((u) => u.latency_ms) };
}

export const isRunning = (run: CompareRun | undefined): boolean => run !== undefined && !run.finished;

// --- Session history ---------------------------------------------------------------------

const HISTORY_KEY = "compare-history";
export const MAX_HISTORY = 20;

type Store = Pick<Storage, "getItem" | "setItem">;

export function loadHistory(storage: Store): CompareRun[] {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(HISTORY_KEY) ?? "[]");
    return Array.isArray(parsed) ? (parsed as CompareRun[]).filter((r) => r.finished) : [];
  } catch {
    return [];
  }
}

export function saveHistory(storage: Store, runs: CompareRun[]): void {
  try {
    storage.setItem(HISTORY_KEY, JSON.stringify(runs.filter((r) => r.finished).slice(0, MAX_HISTORY)));
  } catch {
    // Storage may be full or disabled; the history simply stays in memory.
  }
}
