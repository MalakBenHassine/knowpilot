/**
 * A Server-Sent Events reader for a fetch() body.
 *
 * Why not `EventSource`, the browser's built-in client: it can only send GET,
 * with no body and no custom header. Our stream is a POST carrying the
 * question and the X-CSRF-Token header - the same protection as every other
 * state-changing request. So the stream is read with fetch, and parsed here.
 *
 * The format (https://html.spec.whatwg.org/multipage/server-sent-events.html):
 * events are separated by a blank line; each line is `field: value`; several
 * `data:` lines are joined with "\n"; a line starting with ":" is a comment.
 */
export interface ServerSentEvent {
  event: string
  data: string
}

/** Blank line between events, whatever the line ending. */
const EVENT_BOUNDARY = /\r\n\r\n|\n\n|\r\r/

export async function* readServerSentEvents(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<ServerSentEvent> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      // A network chunk can end in the middle of an event - or in the middle
      // of a multi-byte character: `stream: true` keeps the incomplete bytes
      // until the next chunk instead of emitting a replacement character.
      // Only complete events are parsed; the remainder waits.
      buffer += decoder.decode(value, { stream: true })
      let match = EVENT_BOUNDARY.exec(buffer)
      while (match) {
        const block = buffer.slice(0, match.index)
        buffer = buffer.slice(match.index + match[0].length)
        const parsed = parseBlock(block)
        if (parsed) yield parsed
        match = EVENT_BOUNDARY.exec(buffer)
      }
    }
    // A last event without its trailing blank line is still an event.
    const parsed = parseBlock(buffer)
    if (parsed) yield parsed
  } finally {
    // cancel(), not releaseLock(): if the consumer leaves early, the download
    // must actually stop - the server then closes the provider's stream, and
    // the generation nobody will read stops being paid for. After a normal end
    // it is a no-op.
    await reader.cancel().catch(() => undefined)
  }
}

function parseBlock(block: string): ServerSentEvent | null {
  let event = 'message'
  const data: string[] = []
  for (const line of block.split(/\r\n|\n|\r/)) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    // One optional space after the colon belongs to the syntax, not the value.
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') event = value
    else if (field === 'data') data.push(value)
  }
  return data.length > 0 ? { event, data: data.join('\n') } : null
}
