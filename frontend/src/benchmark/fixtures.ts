/** Sample data shared by the tests (and mirrored by the screenshot mock). */

import type { ApproachStatus, BenchmarkQuestion, BenchmarkStatus, ResultRow } from "./types";

export const QUESTIONS: BenchmarkQuestion[] = [
  {
    id: "summary-data-source",
    category: "data-source",
    question: "Where does the customer summary come from?",
    expected_answer: "It reads the **orders** table and caches for 60 s.",
    key_facts: ["orders", "60 | 1 minute"],
    evidence: ["app/routers/customers.py"],
  },
  {
    id: "tax-rate",
    category: "business-logic",
    question: "What tax rate is applied to an order?",
    expected_answer: "8.25 percent.",
    key_facts: ["0.0825 | 8.25%"],
    evidence: ["app/services/pricing.py"],
  },
];

export function row(overrides: Partial<ResultRow> = {}): ResultRow {
  return {
    approach: "tools-only",
    question_id: "summary-data-source",
    category: "data-source",
    answer: "It reads the orders table.",
    facts_matched: 2,
    facts_total: 2,
    matched_facts: ["orders", "60 | 1 minute"],
    missing_facts: [],
    correct: true,
    judge_correct: null,
    judge_reason: "",
    input_tokens: 2300,
    output_tokens: 170,
    total_tokens: 2470,
    cache_read_tokens: 800,
    cache_creation_tokens: 0,
    llm_calls: 2,
    tool_calls: 1,
    latency_ms: 8400,
    tools_used: ["search_code"],
    error: "",
    ...overrides,
  };
}

export const APPROACHES: ApproachStatus[] = [
  { name: "json-github", summary: "Index + GitHub API", available: false, reason: "GITHUB_TOKEN is not set on the server" },
  { name: "json-metadata", summary: "Index + metadata", available: true, reason: null },
  { name: "tools-only", summary: "Just the tools", available: true, reason: null },
  { name: "json-embeddings", summary: "Index + embeddings", available: true, reason: null },
];

export const STATUS: BenchmarkStatus = {
  llm_configured: true,
  llm_message: null,
  llm_model: "claude-test",
  approaches: APPROACHES,
  repositories: ["shop-api", "shop-web"],
  question_count: 2,
  github_ref: "main",
  running_run_id: null,
};
