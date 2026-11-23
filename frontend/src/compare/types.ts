/** Shapes mirror backend/src/repo_analyzer/api/compare_routes.py and benchmark/compare.py. */

import type { Usage } from "../usage";

export interface CompareApproach {
  name: string;
  /** Heading shown on the column (for example "Chat agent"). Defaults to the approach name. */
  label?: string;
  summary: string;
  available: boolean;
  /** Why the approach cannot run (null when it can). */
  reason: string | null;
}

export interface CompareRepository {
  id: string;
  label: string;
  kind: "fixtures" | "workspace";
}

export interface CompareStatus {
  llm_configured: boolean;
  llm_message: string | null;
  llm_model: string;
  repositories: CompareRepository[];
  approaches: CompareApproach[];
}

/** One-off work an approach paid for before answering (endpoint metadata, embeddings). */
export interface SetupCost {
  description: string;
  total_tokens: number;
}

export type CompareEvent =
  | { type: "plan"; question: string; repository: string; label: string; approaches: CompareApproach[] }
  | { type: "setup"; approach: string; message: string }
  | { type: "started"; approach: string }
  | { type: "token"; approach: string; text: string }
  | { type: "tool_call"; approach: string; name: string; args: Record<string, unknown> }
  | { type: "error"; approach: string; message: string }
  | { type: "unavailable"; approach: string; reason: string }
  | { type: "usage"; approach: string; usage: Usage; setup: SetupCost | null }
  | { type: "approach_done"; approach: string }
  | { type: "done" };

export interface CompareRequest {
  question: string;
  repository: string;
  approaches?: string[];
}
