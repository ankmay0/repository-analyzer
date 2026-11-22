/** Shapes mirror backend/src/repo_analyzer/api/benchmark_routes.py and benchmark/service.py. */

export interface ApproachStatus {
  name: string;
  summary: string;
  available: boolean;
  /** Why the approach cannot run on this server (null when it can). */
  reason: string | null;
}

export interface BenchmarkStatus {
  llm_configured: boolean;
  llm_message: string | null;
  llm_model: string;
  approaches: ApproachStatus[];
  repositories: string[];
  question_count: number;
  github_ref: string;
  running_run_id: string | null;
}

export interface BenchmarkQuestion {
  id: string;
  category: string;
  question: string;
  expected_answer: string;
  key_facts: string[];
  evidence: string[];
}

export interface ResultRow {
  approach: string;
  question_id: string;
  category: string;
  answer: string;
  facts_matched: number;
  facts_total: number;
  matched_facts: string[];
  missing_facts: string[];
  correct: boolean;
  judge_correct: boolean | null;
  judge_reason: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  llm_calls: number;
  tool_calls: number;
  latency_ms: number;
  tools_used: string[];
  error: string;
}

export type RunState = "running" | "finished" | "failed";

export interface RunSnapshot {
  id: string;
  state: RunState;
  approaches: string[];
  question_ids: string[];
  repositories: string[];
  judge: boolean;
  github_ref: string;
  total: number;
  completed: number;
  current: RunCurrent | null;
  rows: ResultRow[];
  skipped: Record<string, string>;
  error: string | null;
  next_event: number;
}

export interface RunCurrent {
  approach: string;
  question_id: string | null;
  phase: "setup" | "asking";
}

export interface Progress {
  completed: number;
  total: number;
}

export type RunEvent =
  | { type: "plan"; approaches: string[]; question_ids: string[]; total: number; progress: Progress }
  | { type: "approach_setup"; approach: string; progress: Progress }
  | { type: "approach_started"; approach: string; progress: Progress }
  | { type: "approach_skipped"; approach: string; reason: string; progress: Progress }
  | { type: "question_started"; approach: string; question_id: string; progress: Progress }
  | { type: "result"; result: ResultRow; progress: Progress }
  | { type: "finished"; progress: Progress }
  | { type: "failed"; error: string; progress: Progress };

export interface RunRequest {
  approaches: string[];
  question_ids?: string[];
  judge?: boolean;
  github_ref?: string;
}
