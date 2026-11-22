import type { StreamEvent } from "./types";

/**
 * POST a chat message and yield the server's SSE events one by one.
 *
 * The browser's built-in EventSource only supports GET, and we need to POST a JSON body,
 * so we read the response stream ourselves and parse the SSE format:
 *   event: <type>\n
 *   data: <json>\n
 *   \n                     <- a blank line ends one event
 */
export async function* streamChat(message: string, signal: AbortSignal): AsyncGenerator<StreamEvent> {
  const resp = await fetch("/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value.replace(/\r\n/g, "\n");
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const event = parseBlock(block);
      if (event) yield event;
    }
  }
}

function parseBlock(block: string): StreamEvent | null {
  let type = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null; // keep-alive pings carry no data
  try {
    // The server is the source of truth for the shape; types.ts documents it.
    return { type, data: JSON.parse(data) } as StreamEvent;
  } catch {
    console.warn("unparseable SSE event", type, data);
    return null;
  }
}
