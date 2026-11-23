import { ApiError, JSON_HEADERS, request, toApiError } from "../api";
import { parseServerSentEvents } from "../sse";
import { parseUsage } from "../usage";
import type { CompareEvent, CompareRequest, CompareStatus } from "./types";

export function getCompareStatus(signal?: AbortSignal): Promise<CompareStatus> {
  return request<CompareStatus>("/api/compare/status", { signal });
}

/** Ask one question under every approach and yield the tagged events as they arrive. */
export async function* streamCompare(
  body: CompareRequest,
  signal: AbortSignal,
): AsyncGenerator<CompareEvent> {
  const response = await fetch("/api/compare", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw await toApiError(response);
  if (!response.body) throw new ApiError("The server sent an empty response", response.status);

  for await (const { data } of parseServerSentEvents(response.body)) {
    const event = JSON.parse(data) as CompareEvent;
    // Token counts are validated like the chat's usage event: bad fields become zero.
    yield event.type === "usage"
      ? { ...event, usage: parseUsage(event.usage as unknown as Record<string, unknown>) }
      : event;
    if (event.type === "done") return;
  }
}
