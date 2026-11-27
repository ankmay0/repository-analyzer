/** Typed client for the backend API. Shapes mirror backend/src/repo_analyzer/api/schemas.py. */

import { parseServerSentEvents } from "./sse";
import { parseUsage, type Usage } from "./usage";

export type { Usage } from "./usage";

export interface Repository {
  name: string;
  owner: string;
  source: string;
  commit: string | null;
  default_branch: string | null;
  file_count: number;
  symbol_count: number;
  dependency_count: number;
  /** Name of this repository's JSON index file in the server's data directory. */
  index_file: string;
}

export interface Job {
  state: "idle" | "running" | "succeeded" | "failed";
  message: string;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Workspace {
  repositories: Repository[];
  indexed_at: string | null;
  job: Job;
}

/** Liveness and which optional services the server can use. Mirrors HealthOut. */
export interface Health {
  status: string;
  llm_configured: boolean;
  llm_model: string;
  ctags_available: boolean;
  ctags_version: string | null;
  /** RA_GRAPH_ENABLED is on and Neo4j is reachable. */
  graph_available: boolean;
}

export function parseHealth(value: unknown): Health {
  const body = typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  return {
    status: String(body.status ?? "ok"),
    llm_configured: body.llm_configured === true,
    llm_model: String(body.llm_model ?? ""),
    ctags_available: body.ctags_available === true,
    ctags_version: typeof body.ctags_version === "string" ? body.ctags_version : null,
    graph_available: body.graph_available === true,
  };
}

export interface Source {
  repo: string;
  path: string;
  start_line: number;
  end_line: number;
}

export type ChatEvent =
  | { type: "token"; text: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "sources"; sources: Source[] }
  | { type: "error"; message: string }
  | { type: "usage"; usage: Usage }
  | { type: "done" };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function toApiError(response: Response): Promise<ApiError> {
  let detail = `Request failed (HTTP ${response.status})`;
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null && "detail" in body) {
      const value = (body as { detail: unknown }).detail;
      // FastAPI reports validation errors as a list of objects with a `msg` field.
      detail = Array.isArray(value)
        ? value.map((item) => (item as { msg?: string }).msg ?? "invalid input").join("; ")
        : String(value);
    }
  } catch {
    // Not a JSON body (for example a proxy error page); keep the generic message.
  }
  return new ApiError(detail, response.status);
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) throw await toApiError(response);
  return (await response.json()) as T;
}

export const JSON_HEADERS = { "Content-Type": "application/json" };

export async function getHealth(signal?: AbortSignal): Promise<Health> {
  return parseHealth(await request<unknown>("/api/health", { signal }));
}

export function getWorkspace(signal?: AbortSignal): Promise<Workspace> {
  return request<Workspace>("/api/workspace", { signal });
}

export function startIndexing(repositories: string[]): Promise<Workspace> {
  return request<Workspace>("/api/workspace/index", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ repositories }),
  });
}

/** Ask a question and yield the answer as it is produced. Abort `signal` to stop. */
export async function* streamChat(
  threadId: string,
  message: string,
  signal: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ thread_id: threadId, message }),
    signal,
  });
  if (!response.ok) throw await toApiError(response);
  if (!response.body) throw new ApiError("The server sent an empty response", response.status);

  for await (const { event, data } of parseServerSentEvents(response.body)) {
    const payload = JSON.parse(data) as Record<string, unknown>;
    switch (event) {
      case "token":
        yield { type: "token", text: String(payload.text ?? "") };
        break;
      case "tool_call":
        yield {
          type: "tool_call",
          name: String(payload.name),
          args: (payload.args ?? {}) as Record<string, unknown>,
        };
        break;
      case "sources":
        yield { type: "sources", sources: (payload.sources ?? []) as Source[] };
        break;
      case "error":
        yield { type: "error", message: String(payload.message ?? "Unknown error") };
        break;
      case "usage":
        yield { type: "usage", usage: parseUsage(payload) };
        break;
      case "done":
        yield { type: "done" };
        return;
      default:
        break; // Unknown event types are ignored so the server can add new ones.
    }
  }
}


export interface DirectToolCall {
  name: string;
  arguments: Record<string, unknown>;
  result: string | null;
}

export interface DirectAskResponse {
  repo: string;
  question: string;
  answer: string;
  tool_calls: DirectToolCall[];
  /** Present only if the server includes a usage object; otherwise null. */
  usage: Usage | null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** Read a `/api/direct/ask` body, ignoring unknown fields. */
export function parseDirectAsk(value: unknown): DirectAskResponse {
  const body = asRecord(value);
  const rawCalls = Array.isArray(body.tool_calls) ? body.tool_calls : [];
  const tool_calls: DirectToolCall[] = rawCalls.map((item) => {
    const call = asRecord(item);
    const args = call.arguments ?? call.args;
    return {
      name: String(call.name ?? ""),
      arguments: asRecord(args),
      result: typeof call.result === "string" ? call.result : null,
    };
  });
  const usageRaw = body.usage;
  return {
    repo: String(body.repo ?? ""),
    question: String(body.question ?? ""),
    answer: String(body.answer ?? ""),
    tool_calls,
    usage: typeof usageRaw === "object" && usageRaw !== null ? parseUsage(asRecord(usageRaw)) : null,
  };
}

/** Ask the direct agent (no JSON index). Abort `signal` to cancel the request. */
export async function askDirect(
  repo: string,
  question: string,
  signal?: AbortSignal,
): Promise<DirectAskResponse> {
  return parseDirectAsk(
    await request<unknown>("/api/direct/ask", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ repo, question }),
      signal,
    }),
  );
}
