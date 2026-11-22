import { ApiError, JSON_HEADERS, request, toApiError } from "../api";
import { parseServerSentEvents } from "../sse";
import type {
  BenchmarkQuestion,
  BenchmarkStatus,
  RunEvent,
  RunRequest,
  RunSnapshot,
} from "./types";

export function getBenchmarkStatus(signal?: AbortSignal): Promise<BenchmarkStatus> {
  return request<BenchmarkStatus>("/api/benchmark/status", { signal });
}

export function getBenchmarkQuestions(
  signal?: AbortSignal,
): Promise<{ repositories: string[]; questions: BenchmarkQuestion[] }> {
  return request("/api/benchmark/questions", { signal });
}

export function startBenchmarkRun(body: RunRequest): Promise<RunSnapshot> {
  return request<RunSnapshot>("/api/benchmark/runs", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
}

export function getBenchmarkRun(id: string, signal?: AbortSignal): Promise<RunSnapshot> {
  return request<RunSnapshot>(`/api/benchmark/runs/${encodeURIComponent(id)}`, { signal });
}

export function benchmarkCsvUrl(id: string): string {
  return `/api/benchmark/runs/${encodeURIComponent(id)}/results.csv`;
}

/** Stream a run's progress events, starting after the first `after` events. */
export async function* streamBenchmarkEvents(
  id: string,
  after: number,
  signal: AbortSignal,
): AsyncGenerator<RunEvent> {
  const response = await fetch(
    `/api/benchmark/runs/${encodeURIComponent(id)}/events?after=${after}`,
    { signal },
  );
  if (!response.ok) throw await toApiError(response);
  if (!response.body) throw new ApiError("The server sent an empty response", response.status);
  for await (const { data } of parseServerSentEvents(response.body)) {
    yield JSON.parse(data) as RunEvent;
  }
}
