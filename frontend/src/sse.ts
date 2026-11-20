/**
 * Minimal parser for a `text/event-stream` response body.
 *
 * The browser's EventSource only supports GET, and the chat endpoint is a
 * POST, so the stream is read with fetch and parsed here. Only the `event`
 * and `data` fields are used by this application.
 */

export interface ServerSentEvent {
  event: string;
  data: string;
}

export async function* parseServerSentEvents(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<ServerSentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      // A stream may use \n, \r\n or \r line endings; normalise before splitting.
      // A trailing \r is kept back in case its \n arrives in the next chunk.
      const endsWithCarriageReturn = !done && buffer.endsWith("\r");
      const text = (endsWithCarriageReturn ? buffer.slice(0, -1) : buffer).replace(
        /\r\n?/g,
        "\n",
      );
      const blocks = text.split("\n\n");
      // The last element is an incomplete event until a blank line follows it.
      buffer = (blocks.pop() ?? "") + (endsWithCarriageReturn ? "\r" : "");
      for (const block of blocks) {
        const event = parseBlock(block);
        if (event) yield event;
      }
      if (done) return;
    }
  } finally {
    reader.releaseLock();
  }
}

function parseBlock(block: string): ServerSentEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // blank or comment (keep-alive)
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length > 0 ? { event, data: data.join("\n") } : null;
}
