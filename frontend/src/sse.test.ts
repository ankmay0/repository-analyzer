import { describe, expect, it } from "vitest";

import { parseServerSentEvents, type ServerSentEvent } from "./sse";

function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(...chunks: string[]): Promise<ServerSentEvent[]> {
  const events: ServerSentEvent[] = [];
  for await (const event of parseServerSentEvents(streamOf(...chunks))) events.push(event);
  return events;
}

describe("parseServerSentEvents", () => {
  it("parses consecutive events", async () => {
    expect(await collect('event: token\ndata: {"text":"a"}\n\nevent: done\ndata: {}\n\n')).toEqual([
      { event: "token", data: '{"text":"a"}' },
      { event: "done", data: "{}" },
    ]);
  });

  it("reassembles events split across network chunks", async () => {
    expect(await collect("event: to", 'ken\ndata: {"te', 'xt":"a"}\n', "\nevent: done\ndata: {}\n\n")).toEqual([
      { event: "token", data: '{"text":"a"}' },
      { event: "done", data: "{}" },
    ]);
  });

  it("handles CRLF line endings, including a CRLF split across chunks", async () => {
    expect(await collect("event: token\r\ndata: 1\r", "\n\r\nevent: done\r\ndata: 2\r\n\r\n")).toEqual([
      { event: "token", data: "1" },
      { event: "done", data: "2" },
    ]);
  });

  it("ignores keep-alive comments and joins multi-line data", async () => {
    expect(await collect(": ping\n\nevent: token\ndata: line one\ndata: line two\n\n")).toEqual([
      { event: "token", data: "line one\nline two" },
    ]);
  });

  it("does not split multi-byte characters across chunks", async () => {
    const bytes = new TextEncoder().encode("data: héllo\n\n");
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 8)); // cuts "é" in half
        controller.enqueue(bytes.slice(8));
        controller.close();
      },
    });
    const events: ServerSentEvent[] = [];
    for await (const event of parseServerSentEvents(stream)) events.push(event);
    expect(events).toEqual([{ event: "message", data: "héllo" }]);
  });

  it("discards an event cut off before its terminating blank line, as the SSE spec requires", async () => {
    expect(await collect("event: token\ndata: 1\n\nevent: done\ndata: {}")).toEqual([
      { event: "token", data: "1" },
    ]);
  });
});
