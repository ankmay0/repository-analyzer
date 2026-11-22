import { describe, expect, it } from "vitest";

import { QUESTIONS, row } from "./fixtures";
import {
  applyEvent,
  formatSeconds,
  formatTokens,
  gradeOf,
  summarize,
  toggleId,
  viewFromSnapshot,
  type RunView,
} from "./model";
import type { RunEvent, RunSnapshot } from "./types";

const snapshot: RunSnapshot = {
  id: "run1",
  state: "running",
  approaches: ["tools-only", "json-metadata"],
  question_ids: QUESTIONS.map((q) => q.id),
  repositories: ["shop-api"],
  judge: false,
  github_ref: "main",
  total: 4,
  completed: 0,
  current: null,
  rows: [],
  skipped: {},
  error: null,
  next_event: 1,
};

describe("gradeOf", () => {
  it("is pass when every key fact is found, partial when some are, fail otherwise", () => {
    expect(gradeOf(row())).toBe("pass");
    expect(gradeOf(row({ correct: false, facts_matched: 1 }))).toBe("partial");
    expect(gradeOf(row({ correct: false, facts_matched: 0, error: "boom" }))).toBe("fail");
  });
});

describe("summarize", () => {
  it("averages tokens and latency and counts grades per approach, in order", () => {
    const rows = [
      row({ approach: "tools-only", total_tokens: 2000, latency_ms: 4000 }),
      row({ approach: "tools-only", question_id: "tax-rate", correct: false, facts_matched: 1, facts_total: 2, total_tokens: 4000, latency_ms: 8000 }),
      row({ approach: "json-metadata", total_tokens: 1000, latency_ms: 2000 }),
    ];

    const [tools, metadata, ...rest] = summarize(rows, ["tools-only", "json-metadata", "json-github"]);

    expect(rest).toEqual([]); // json-github has no answers yet
    expect(tools).toMatchObject({
      approach: "tools-only",
      answered: 2,
      pass: 1,
      partial: 1,
      fail: 0,
      correctPct: 50,
      factPct: 75,
      avgTokens: 3000,
      avgLatencyMs: 6000,
    });
    expect(metadata).toMatchObject({ approach: "json-metadata", correctPct: 100, avgTokens: 1000 });
  });
});

describe("applyEvent", () => {
  it("builds the live view from a stream of events and ignores replayed results", () => {
    const first = row();
    const second = row({ question_id: "tax-rate", correct: false, facts_matched: 0 });
    const events: RunEvent[] = [
      { type: "plan", approaches: snapshot.approaches, question_ids: snapshot.question_ids, total: 4, progress: { completed: 0, total: 4 } },
      { type: "approach_setup", approach: "json-metadata", progress: { completed: 0, total: 4 } },
      { type: "question_started", approach: "tools-only", question_id: "summary-data-source", progress: { completed: 0, total: 4 } },
      { type: "result", result: first, progress: { completed: 1, total: 4 } },
      { type: "result", result: first, progress: { completed: 1, total: 4 } },
      { type: "result", result: second, progress: { completed: 2, total: 4 } },
      { type: "approach_skipped", approach: "json-metadata", reason: "no metadata", progress: { completed: 2, total: 2 } },
      { type: "finished", progress: { completed: 2, total: 2 } },
    ];

    const view = events.reduce<RunView>(applyEvent, viewFromSnapshot(snapshot));

    expect(view.rows).toEqual([first, second]);
    expect(view.progress).toEqual({ completed: 2, total: 2 });
    expect(view.skipped).toEqual({ "json-metadata": "no metadata" });
    expect(view.state).toBe("finished");
    expect(view.current).toBeNull();
  });

  it("records why a run failed", () => {
    const view = applyEvent(viewFromSnapshot(snapshot), {
      type: "failed",
      error: "RuntimeError: boom",
      progress: { completed: 0, total: 4 },
    });
    expect(view).toMatchObject({ state: "failed", error: "RuntimeError: boom" });
  });
});

describe("formatting and selection", () => {
  it("formats tokens and seconds compactly", () => {
    expect(formatTokens(842)).toBe("842");
    expect(formatTokens(2470)).toBe("2.5k");
    expect(formatSeconds(8400)).toBe("8.4 s");
  });

  it("toggles ids without mutating the original set", () => {
    const original = new Set(["a"]);
    expect([...toggleId(original, "b")]).toEqual(["a", "b"]);
    expect([...toggleId(original, "a")]).toEqual([]);
    expect([...original]).toEqual(["a"]);
  });
});
