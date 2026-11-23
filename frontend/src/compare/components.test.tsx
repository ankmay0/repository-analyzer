import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { streamCompare } from "./api";
import { CompareColumn } from "./CompareColumn";
import { ComparePage } from "./ComparePage";
import { plan, usage } from "./fixtures";
import { applyCompareEvent, emptyColumn, newRun, type ColumnView } from "./model";
import type { CompareEvent } from "./types";

const done: ColumnView = {
  ...emptyColumn({ name: "tools-only", summary: "No index", available: true, reason: null }),
  state: "done",
  text: "The tax rate is **8.25%**.",
  toolCalls: ["list_files", "read_file"],
  usage: usage({ total_tokens: 12345, input_tokens: 12000, output_tokens: 345, latency_ms: 14200, tool_calls: 2, llm_calls: 3 }),
  setup: { description: "endpoint metadata", total_tokens: 10600 },
};

const html = (column: ColumnView, fewestTokens = false, fastest = false) =>
  renderToStaticMarkup(<CompareColumn column={column} fewestTokens={fewestTokens} fastest={fastest} />);

describe("CompareColumn", () => {
  it("renders the markdown answer with a token and latency footer", () => {
    const out = html(done);
    expect(out).toContain("<strong>8.25%</strong>");
    expect(out).toContain("12,345");
    expect(out).toContain("12,000 in · 345 out · 800 cached");
    expect(out).toContain("14.2 s");
    expect(out).toContain("2 tool calls · 3 model calls");
    expect(out).toContain("+ 10,600 one-off setup tokens (endpoint metadata)");
  });

  it("shows the Fewest tokens and Fastest badges only when asked", () => {
    expect(html(done)).not.toContain("compare-badge");
    const out = html(done, true, true);
    expect(out).toContain("Fewest tokens");
    expect(out).toContain("Fastest");
  });

  it("greys out an unavailable approach, shows why, and has no usage footer", () => {
    const out = html(emptyColumn({ name: "json-github", summary: "x", available: false, reason: "GITHUB_TOKEN is not set on the server" }));
    expect(out).toContain("compare-unavailable");
    expect(out).toContain('aria-disabled="true"');
    expect(out).toContain("Unavailable: GITHUB_TOKEN is not set on the server");
    expect(out).not.toContain("compare-footer");
  });

  it("shows progress while preparing and an error message when it fails", () => {
    const preparing = { ...done, state: "preparing" as const, text: "", usage: null, setup: null, setupMessage: "Preparing endpoint metadata" };
    expect(html(preparing)).toContain("Preparing endpoint metadata…");
    const failed = { ...done, state: "error" as const, usage: null, error: "Unexpected server error" };
    const out = html(failed);
    expect(out).toContain('role="alert"');
    expect(out).toContain("No usage reported");
  });
});

describe("ComparePage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows past questions from this session and the latest result side by side", () => {
    const events: CompareEvent[] = [
      plan(),
      { type: "token", approach: "tools-only", text: "Eight and a quarter percent." },
      { type: "usage", approach: "tools-only", usage: usage({ total_tokens: 9000, latency_ms: 9000 }), setup: null },
      { type: "approach_done", approach: "tools-only" },
      { type: "token", approach: "json-metadata", text: "8.25%" },
      { type: "usage", approach: "json-metadata", usage: usage({ total_tokens: 3000, latency_ms: 4000 }), setup: null },
      { type: "approach_done", approach: "json-metadata" },
      { type: "done" },
    ];
    const run = events.reduce(applyCompareEvent, newRun(1, "How is an order priced?", "benchmark-fixtures", "fixtures"));
    vi.stubGlobal("sessionStorage", {
      getItem: () => JSON.stringify([run, { ...run, id: 0, question: "An earlier question" }]),
      setItem: () => undefined,
    });

    const out = renderToStaticMarkup(<ComparePage active={false} />);

    expect(out).toContain("This session");
    expect(out).toContain("An earlier question");
    expect(out.match(/class="compare-column /g)).toHaveLength(4);
    expect(out).toContain("Unavailable: GITHUB_TOKEN is not set on the server");
    // json-metadata used fewer tokens and was faster than tools-only.
    expect(out.match(/Fewest tokens/g)).toHaveLength(1);
    expect(out.match(/Fastest/g)).toHaveLength(1);
    expect(out.indexOf("Fewest tokens")).toBeGreaterThan(out.indexOf('data-approach="json-metadata"'));
  });
});

describe("streamCompare", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the question and yields tagged events, reading usage defensively", async () => {
    const body = [
      'event: plan\ndata: {"type":"plan","question":"q","repository":"r","label":"R","approaches":[]}\n\n',
      'event: usage\ndata: {"type":"usage","approach":"tools-only","usage":{"input_tokens":5,"output_tokens":"bad"},"setup":null}\n\n',
      'event: done\ndata: {"type":"done"}\n\n',
    ].join("");
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
    vi.stubGlobal("fetch", fetchMock);

    const events: CompareEvent[] = [];
    for await (const event of streamCompare({ question: "q", repository: "r" }, new AbortController().signal)) events.push(event);

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/compare");
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1].body)).toEqual({ question: "q", repository: "r" });
    expect(events.map((e) => e.type)).toEqual(["plan", "usage", "done"]);
    const usageEvent = events[1];
    expect(usageEvent?.type === "usage" && usageEvent.usage).toMatchObject({ input_tokens: 5, output_tokens: 0, total_tokens: 0 });
  });

  it("turns an error status into an ApiError with the server's message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "ANTHROPIC_API_KEY is not set" }), { status: 503 })));
    const read = async () => {
      for await (const event of streamCompare({ question: "q", repository: "r" }, new AbortController().signal)) void event;
    };
    await expect(read()).rejects.toMatchObject({ status: 503, message: "ANTHROPIC_API_KEY is not set" });
  });
});
