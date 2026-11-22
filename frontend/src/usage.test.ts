import { describe, expect, it } from "vitest";

import { formatLatency, formatUsage, parseUsage } from "./usage";

describe("parseUsage", () => {
  it("reads every field", () => {
    expect(
      parseUsage({
        input_tokens: 4400,
        output_tokens: 95,
        total_tokens: 4495,
        cache_read_tokens: 2400,
        cache_creation_tokens: 1000,
        llm_calls: 3,
        tool_calls: 2,
        latency_ms: 5320,
      }),
    ).toEqual({
      input_tokens: 4400,
      output_tokens: 95,
      total_tokens: 4495,
      cache_read_tokens: 2400,
      cache_creation_tokens: 1000,
      llm_calls: 3,
      tool_calls: 2,
      latency_ms: 5320,
    });
  });

  it("treats missing or malformed values as zero", () => {
    const usage = parseUsage({ input_tokens: "12", output_tokens: -1, total_tokens: null });
    expect(usage.input_tokens).toBe(0);
    expect(usage.output_tokens).toBe(0);
    expect(usage.total_tokens).toBe(0);
    expect(usage.latency_ms).toBe(0);
  });
});

describe("formatUsage", () => {
  it("summarises tokens, cache, calls and latency", () => {
    expect(
      formatUsage(
        parseUsage({
          input_tokens: 4400,
          output_tokens: 95,
          total_tokens: 4495,
          cache_read_tokens: 2400,
          cache_creation_tokens: 1000,
          llm_calls: 3,
          tool_calls: 2,
          latency_ms: 5320,
        }),
      ),
    ).toBe(
      "4,495 tokens (4,400 in · 95 out) · cache 2,400 read · 1,000 written · 3 model calls · 2 tool calls · 5.3 s",
    );
  });

  it("omits the cache part when nothing was cached and uses singulars", () => {
    expect(
      formatUsage(
        parseUsage({ input_tokens: 1, output_tokens: 0, total_tokens: 1, llm_calls: 1, tool_calls: 1, latency_ms: 850 }),
      ),
    ).toBe("1 token (1 in · 0 out) · 1 model call · 1 tool call · 850 ms");
  });
});

describe("formatLatency", () => {
  it("switches to seconds at one second", () => {
    expect(formatLatency(999)).toBe("999 ms");
    expect(formatLatency(1000)).toBe("1.0 s");
  });
});
