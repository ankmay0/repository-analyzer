/** Token and latency summary for one chat answer (the `usage` server-sent event). */
export interface Usage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  llm_calls: number;
  tool_calls: number;
  latency_ms: number;
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.round(value) : 0;
}

/** Read a `usage` event payload, treating missing or malformed fields as zero. */
export function parseUsage(payload: Record<string, unknown>): Usage {
  return {
    input_tokens: count(payload.input_tokens),
    output_tokens: count(payload.output_tokens),
    total_tokens: count(payload.total_tokens),
    cache_read_tokens: count(payload.cache_read_tokens),
    cache_creation_tokens: count(payload.cache_creation_tokens),
    llm_calls: count(payload.llm_calls),
    tool_calls: count(payload.tool_calls),
    latency_ms: count(payload.latency_ms),
  };
}

const NUMBER = new Intl.NumberFormat("en-US");

function plural(n: number, word: string): string {
  return `${NUMBER.format(n)} ${word}${n === 1 ? "" : "s"}`;
}

export function formatLatency(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** One-line summary shown under an answer, e.g. "4,495 tokens (4,400 in · 95 out) · …". */
export function formatUsage(usage: Usage): string {
  const parts = [
    `${plural(usage.total_tokens, "token")} (${NUMBER.format(usage.input_tokens)} in · ${NUMBER.format(usage.output_tokens)} out)`,
  ];
  if (usage.cache_read_tokens || usage.cache_creation_tokens) {
    parts.push(
      `cache ${NUMBER.format(usage.cache_read_tokens)} read · ${NUMBER.format(usage.cache_creation_tokens)} written`,
    );
  }
  parts.push(plural(usage.llm_calls, "model call"));
  parts.push(plural(usage.tool_calls, "tool call"));
  parts.push(formatLatency(usage.latency_ms));
  return parts.join(" · ");
}
