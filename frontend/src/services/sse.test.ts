import { describe, expect, it } from 'vitest'
import { readServerSentEvents, type ServerSentEvent } from './sse'

/** A body that arrives in the given pieces, the way a network delivers it. */
function bodyOf(...pieces: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const piece of pieces) {
        controller.enqueue(typeof piece === 'string' ? encoder.encode(piece) : piece)
      }
      controller.close()
    },
  })
}

async function read(body: ReadableStream<Uint8Array>): Promise<ServerSentEvent[]> {
  const events: ServerSentEvent[] = []
  for await (const event of readServerSentEvents(body)) events.push(event)
  return events
}

describe('readServerSentEvents', () => {
  it('reads named events and their data', async () => {
    const events = await read(bodyOf('event: token\ndata: {"text":"Doc"}\n\nevent: done\ndata: {}\n\n'))

    expect(events).toEqual([
      { event: 'token', data: '{"text":"Doc"}' },
      { event: 'done', data: '{}' },
    ])
  })

  it('reassembles an event cut across network chunks', async () => {
    // The network owes us nothing about where it splits a response.
    const events = await read(bodyOf('event: tok', 'en\ndata: {"te', 'xt":"a"}\n', '\n'))

    expect(events).toEqual([{ event: 'token', data: '{"text":"a"}' }])
  })

  it('reassembles a character cut in half', async () => {
    // "é" is two bytes in UTF-8; a chunk boundary can fall between them.
    const bytes = new TextEncoder().encode('event: token\ndata: é\n\n')
    const cut = bytes.indexOf(0xc3) + 1

    const events = await read(bodyOf(bytes.slice(0, cut), bytes.slice(cut)))

    expect(events).toEqual([{ event: 'token', data: 'é' }])
  })

  it('accepts CRLF line endings', async () => {
    const events = await read(bodyOf('event: done\r\ndata: {}\r\n\r\n'))

    expect(events).toEqual([{ event: 'done', data: '{}' }])
  })

  it('joins multi-line data and ignores comments', async () => {
    const events = await read(bodyOf(': keep-alive\n\ndata: one\ndata: two\n\n'))

    expect(events).toEqual([{ event: 'message', data: 'one\ntwo' }])
  })

  it('keeps a final event that lacks its blank line', async () => {
    const events = await read(bodyOf('event: done\ndata: {}'))

    expect(events).toEqual([{ event: 'done', data: '{}' }])
  })
})
