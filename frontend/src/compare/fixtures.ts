/** Sample data shared by the tests. */

import type { Usage } from "../usage";
import type { CompareApproach, CompareEvent, CompareStatus } from "./types";

export const APPROACHES: CompareApproach[] = [
  { name: "json-github", summary: "Index + GitHub API", available: false, reason: "GITHUB_TOKEN is not set on the server" },
  { name: "json-metadata", summary: "Index + metadata", available: true, reason: null },
  { name: "tools-only", summary: "Just the tools", available: true, reason: null },
  { name: "json-embeddings", summary: "Index + embeddings", available: true, reason: null },
];

export const STATUS: CompareStatus = {
  llm_configured: true,
  llm_message: null,
  llm_model: "claude-test",
  repositories: [
    { id: "benchmark-fixtures", label: "Benchmark fixtures (shop-api + shop-web)", kind: "fixtures" },
    { id: "flask", label: "pallets/flask", kind: "workspace" },
  ],
  approaches: APPROACHES,
};

export function usage(overrides: Partial<Usage> = {}): Usage {
  return {
    input_tokens: 2300,
    output_tokens: 170,
    total_tokens: 2470,
    cache_read_tokens: 800,
    cache_creation_tokens: 0,
    llm_calls: 2,
    tool_calls: 1,
    latency_ms: 8400,
    ...overrides,
  };
}

export function plan(): CompareEvent {
  return {
    type: "plan",
    question: "How is an order priced?",
    repository: "benchmark-fixtures",
    label: "Benchmark fixtures (shop-api + shop-web)",
    approaches: APPROACHES,
  };
}
