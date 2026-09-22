import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { streamQuestion } from './chat'

// The HTTP path, not the mock backend. Scoped to this file so the rest of the
// suite keeps the default mode.
vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  API_MODE: 'http',
}))

const DONE = {
  answer: 'Docker [1]. Jenkins [1].',
  is_grounded: true,
  citations: [
    {
      number: 1,
      document_id: '0b7d3f3e-1c5a-4a9e-9f2e-3a7c1d2e5f80',
      filename: 'cv.pdf',
      page_number: 1,
      snippet: 'Docker, Jenkins.',
    },
  ],
}

function event(name: string, data: unknown): string {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`
}

function serve(body: string, init: ResponseInit = { status: 200 }) {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(body, { headers: { 'Content-Type': 'text/event-stream' }, ...init }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function handlers() {
  return { onGenerating: vi.fn(), onText: vi.fn() }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('streamQuestion', () => {
  it('reports the stage, the growing text, then returns the final answer', async () => {
    serve(
      event('stage', { stage: 'generating' }) +
        event('token', { text: 'Docker [1].' }) +
        event('token', { text: ' Jenkins [1].' }) +
        event('done', DONE),
    )
    const on = handlers()

    const payload = await streamQuestion('Quels outils ?', on)

    expect(on.onGenerating).toHaveBeenCalledOnce()
    // The whole text so far, each time - the component never concatenates.
    expect(on.onText.mock.calls).toEqual([['Docker [1].'], ['Docker [1]. Jenkins [1].']])
    expect(payload).toMatchObject({ outcome: 'answered', answer: DONE.answer })
  })

  it('lets the final verdict override what was streamed', async () => {
    // The rare withdrawal: text was shown, then the server said "not grounded".
    serve(
      event('token', { text: 'Docker [1].' }) +
        event('done', { answer: '', citations: [], is_grounded: false }),
    )

    const payload = await streamQuestion('Quels outils ?', handlers())

    expect(payload).toEqual({ outcome: 'insufficient_evidence' })
  })

  it('turns an error event into the same ApiError as a failed request', async () => {
    serve(event('stage', { stage: 'generating' }) + event('error', { kind: 'rate_limited', retry_after: 1800 }))

    const failure = streamQuestion('Quels outils ?', handlers())

    await expect(failure).rejects.toMatchObject({ kind: 'rate_limited', retryAfterSeconds: 1800 })
  })

  it('tells a saturated service apart from a spent budget', async () => {
    serve(event('stage', { stage: 'generating' }) + event('error', { kind: 'busy', retry_after: 120 }))

    await expect(streamQuestion('Quels outils ?', handlers())).rejects.toMatchObject({
      kind: 'busy',
      retryAfterSeconds: 120,
    })
  })

  it('maps a 503 that says when to come back to busy, and a bare 503 to server', async () => {
    serve('', { status: 503, headers: { 'Retry-After': '120' } })
    await expect(streamQuestion('Quels outils ?', handlers())).rejects.toMatchObject({
      kind: 'busy',
      retryAfterSeconds: 120,
    })

    serve('', { status: 503 })
    await expect(streamQuestion('Quels outils ?', handlers())).rejects.toMatchObject({
      kind: 'server',
    })
  })

  it('treats a stream that ends without a verdict as interrupted', async () => {
    // Whatever was shown is not an answer until the server says so.
    serve(event('token', { text: 'Docker [1].' }))

    await expect(streamQuestion('Quels outils ?', handlers())).rejects.toBeInstanceOf(ApiError)
  })

  it('keeps the rules of every other request: POST, CSRF header, cookie', async () => {
    const fetchMock = serve(event('done', DONE))

    await streamQuestion('Quels outils ?', handlers())

    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe('/api/chat/stream')
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('include')
    expect(init.headers.Accept).toBe('text/event-stream')
    expect(JSON.parse(init.body)).toEqual({ question: 'Quels outils ?' })
  })

  it('maps a refusal before the stream opens like any request', async () => {
    serve('', { status: 429, headers: { 'Retry-After': '60' } })

    await expect(streamQuestion('Quels outils ?', handlers())).rejects.toMatchObject({
      kind: 'rate_limited',
      retryAfterSeconds: 60,
    })
  })
})
