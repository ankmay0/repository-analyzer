/** Typed client for the backend API. Shapes mirror backend/src/repo_analyzer/api/schemas.py. */

import { parseServerSentEvents } from "./sse";

export interface Repository {
  name: string;
  source: string;
  commit: string | null;
  file_count: number;
  chunk_count: number;
}

export type DependencyScope = "runtime" | "optional" | "dev";

export interface Edge {
  source_repo: string;
  target_repo: string;
  ecosystem: string;
  package: string;
  scope: DependencyScope;
  manifest_path: string;
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
  edges: Edge[];
  indexed_at: string | null;
  job: Job;
}

export interface Impacted {
  repo: string;
  distance: number;
  weak: boolean;
  path: Edge[];
}

export interface Impact {
  repo: string;
  impacted: Impacted[];
}

export interface Source {
  repo: string;
  path: string;
  start_line: number;
  end_line: number;
}

export type DiagramNodeKind = "repository" | "file" | "symbol" | "external" | "step" | "other";
export type DiagramNodeStatus = "normal" | "failing" | "affected" | "weak";

export interface DiagramNode {
  id: string;
  label: string;
  kind: DiagramNodeKind;
  status: DiagramNodeStatus;
}

export interface DiagramEdge {
  source: string;
  target: string;
  label: string;
  dashed: boolean;
}

/** Nodes and edges without positions; the UI lays them out. */
export interface Diagram {
  title: string;
  nodes: DiagramNode[];
  edges: DiagramEdge[];
}

export type ChatEvent =
  | { type: "token"; text: string }
  // A step taken by the hub (the coordinating agent), including each delegation.
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  // A search or read performed by the expert on one repository.
  | { type: "expert_step"; repo: string; name: string; args: Record<string, unknown> }
  // A diagram the hub chose to show alongside its answer.
  | { type: "diagram"; diagram: Diagram }
  | { type: "sources"; sources: Source[] }
  | { type: "error"; message: string }
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

async function toApiError(response: Response): Promise<ApiError> {
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) throw await toApiError(response);
  return (await response.json()) as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

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

export function getImpact(repo: string, signal?: AbortSignal): Promise<Impact> {
  return request<Impact>(`/api/workspace/impact/${encodeURIComponent(repo)}`, { signal });
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
      case "expert_step":
        yield {
          type: "expert_step",
          repo: String(payload.repo),
          name: String(payload.name),
          args: (payload.args ?? {}) as Record<string, unknown>,
        };
        break;
      case "diagram":
        yield { type: "diagram", diagram: payload as unknown as Diagram };
        break;
      case "sources":
        yield { type: "sources", sources: (payload.sources ?? []) as Source[] };
        break;
      case "error":
        yield { type: "error", message: String(payload.message ?? "Unknown error") };
        break;
      case "done":
        yield { type: "done" };
        return;
      default:
        break; // Unknown event types are ignored so the server can add new ones.
    }
  }
}
