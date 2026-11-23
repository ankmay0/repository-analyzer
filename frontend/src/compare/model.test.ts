import { describe, expect, it } from "vitest";

import { EMBEDDINGS_UNAVAILABLE, plan, usage } from "./fixtures";
import {
  applyCompareEvent,
  emptyColumn,
  finishRun,
  highlights,
  loadHistory,
  MAX_HISTORY,
  newRun,
  saveHistory,
  type ColumnView,
  type CompareRun,
} from "./model";
import type { CompareEvent } from "./types";

const base = newRun(1, "How is an order priced?", "benchmark-fixtures", "fixtures");

function replay(events: CompareEvent[]): CompareRun {
  return events.reduce(applyCompareEvent, base);
}

const column = (run: CompareRun, name: string): ColumnView => run.columns.find((c) => c.name === name)!;

describe("applyCompareEvent", () => {
  it("starts unavailable approaches greyed out with their reason", () => {
    const run = replay([plan(EMBEDDINGS_UNAVAILABLE)]);
    expect(column(run, "json-embeddings")).toMatchObject({
      state: "unavailable",
      reason: "fastembed is not installed",
    });
    expect(column(run, "tools-only").state).toBe("pending");
  });

  it("streams text, tool calls and usage into the right column only", () => {
    const run = replay([
      plan(),
      { type: "setup", approach: "json-metadata", message: "Preparing endpoint metadata" },
      { type: "started", approach: "tools-only" },
      { type: "tool_call", approach: "tools-only", name: "search_code", args: {} },
      { type: "token", approach: "tools-only", text: "It is " },
      { type: "token", approach: "tools-only", text: "8.25%." },
      { type: "usage", approach: "tools-only", usage: usage(), setup: null },
      { type: "approach_done", approach: "tools-only" },
    ]);

    expect(column(run, "tools-only")).toMatchObject({
      state: "done",
      text: "It is 8.25%.",
      toolCalls: ["search_code"],
    });
    expect(column(run, "tools-only").usage?.total_tokens).toBe(2470);
    expect(column(run, "json-metadata")).toMatchObject({ state: "preparing", text: "" });
    expect(column(run, "json-embeddings").state).toBe("pending");
    expect(run.finished).toBe(false);
  });

  it("keeps an errored approach in the error state even after usage and done", () => {
    const run = replay([
      plan(),
      { type: "token", approach: "tools-only", text: "partial" },
      { type: "error", approach: "tools-only", message: "Unexpected server error" },
      { type: "approach_done", approach: "tools-only" },
      { type: "done" },
    ]);
    expect(column(run, "tools-only")).toMatchObject({ state: "error", error: "Unexpected server error", text: "partial" });
    expect(run.finished).toBe(true);
  });

  it("records an approach that turns out to be unavailable while preparing", () => {
    const run = replay([
      plan(),
      { type: "unavailable", approach: "json-embeddings", reason: "no embedder configured" },
      { type: "approach_done", approach: "json-embeddings" },
    ]);
    expect(column(run, "json-embeddings")).toMatchObject({ state: "unavailable", reason: "no embedder configured" });
  });

  it("closes open columns when the stream ends early", () => {
    const run = finishRun(replay([plan(EMBEDDINGS_UNAVAILABLE), { type: "token", approach: "tools-only", text: "x" }]), "The connection to the server was lost.");
    expect(run.finished).toBe(true);
    expect(column(run, "tools-only")).toMatchObject({ state: "error", error: "The connection to the server was lost." });
    expect(column(run, "json-embeddings").state).toBe("unavailable");
    expect(column(run, "chat")).toMatchObject({ state: "error", error: "The connection to the server was lost." });
  });
});

describe("highlights", () => {
  const done = (name: string, tokens: number, latency: number): ColumnView => ({
    ...emptyColumn({ name, summary: "", available: true, reason: null }),
    state: "done",
    usage: usage({ total_tokens: tokens, latency_ms: latency }),
  });

  it("marks the lowest tokens and the fastest, which can be different approaches", () => {
    const marks = highlights([done("a", 5000, 3000), done("b", 1200, 9000), done("c", 8000, 2000)]);
    expect([...marks.fewestTokens]).toEqual(["b"]);
    expect([...marks.fastest]).toEqual(["c"]);
  });

  it("marks ties, and ignores unavailable, errored and unfinished columns", () => {
    const columns = [
      done("a", 1000, 1000),
      done("b", 1000, 5000),
      { ...done("c", 10, 10), state: "error" as const },
      emptyColumn({ name: "d", summary: "", available: false, reason: "no" }),
    ];
    const marks = highlights(columns);
    expect([...marks.fewestTokens].sort()).toEqual(["a", "b"]);
    expect([...marks.fastest]).toEqual(["a"]);
  });

  it("highlights nothing until two approaches have finished", () => {
    const marks = highlights([done("a", 1, 1), emptyColumn({ name: "b", summary: "", available: true, reason: null })]);
    expect(marks.fewestTokens.size + marks.fastest.size).toBe(0);
  });
});

describe("session history", () => {
  function store() {
    const data = new Map<string, string>();
    return { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) };
  }

  it("round-trips finished runs and drops ones still running", () => {
    const finished = { ...replay([plan(), { type: "done" }]), id: 2 };
    const running = { ...base, id: 3 };
    const s = store();
    saveHistory(s, [running, finished]);
    expect(loadHistory(s)).toEqual([finished]);
  });

  it("keeps only the most recent runs and survives corrupt storage", () => {
    const runs = Array.from({ length: MAX_HISTORY + 5 }, (_, i) => ({ ...base, id: i, finished: true }));
    const s = store();
    saveHistory(s, runs);
    expect(loadHistory(s)).toHaveLength(MAX_HISTORY);
    s.setItem("compare-history", "{not json");
    expect(loadHistory(s)).toEqual([]);
  });
});
