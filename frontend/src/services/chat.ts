import type { AnswerPayload } from '../types/chat'
import type { Source } from '../types/source'
import { API_MODE, ApiError, openStream, request } from './api'
import { mockBackend } from './mockBackend'
import { asBoolean, asNumber, asRecord, asString, InvalidResponseError } from './parse'
import { readServerSentEvents } from './sse'

/**
 * The boundary of the chat feature: wire format in, domain objects out.
 *
 * The backend answers with `is_grounded`, `citations` and snake_case fields.
 * None of that reaches a component: the UI thinks in outcomes and sources, and
 * a change of API shape is a change to this file alone.
 */
export function toSource(raw: unknown): Source {
  const value = asRecord(raw, 'citation')
  const documentId = asString(value.document_id, 'citation.document_id')
  const number = asNumber(value.number, 'citation.number')
  return {
    // Built here rather than sent by the server: the pair is already unique
    // within one answer, and asking the backend for a field only React needs
    // would be one more thing to keep in sync for no benefit.
    id: `${documentId}#${number}`,
    number,
    documentId,
    filename: asString(value.filename, 'citation.filename'),
    page: asNumber(value.page_number, 'citation.page_number'),
    snippet: asString(value.snippet, 'citation.snippet'),
    // `score` is deliberately not mapped. The backend knows the cosine
    // distance, but showing 0.34 to somebody asking about their holidays is
    // noise dressed as precision: it looks authoritative and means nothing to
    // them. The passage itself is the evidence.
  }
}

/**
 * Translates one answer.
 *
 * `is_grounded: false` is a SUCCESSFUL response, not an error: either nothing
 * was close enough to the question, or the model declined to answer from what
 * it was given. Both are the product working, and the UI says so without an
 * error style.
 */
export function toAnswerPayload(raw: unknown): AnswerPayload {
  const value = asRecord(raw, 'answer')
  if (!asBoolean(value.is_grounded, 'answer.is_grounded')) {
    return { outcome: 'insufficient_evidence' }
  }
  if (!Array.isArray(value.citations)) {
    // Grounded means cited, by construction on the server: an answer with no
    // valid citation is refused there. Receiving one here means the contract
    // changed, which is a bug to surface rather than a state to render.
    throw new InvalidResponseError('answer.citations')
  }
  return {
    outcome: 'answered',
    answer: asString(value.answer, 'answer.answer'),
    sources: value.citations.map(toSource),
  }
}

/** What the stream reports while the answer is being produced. */
export interface StreamHandlers {
  /** The server started generating: retrieval is over. */
  onGenerating: () => void
  /** Verified text so far - the whole of it, not only the new piece. */
  onText: (text: string) => void
}

function streamErrorOf(raw: unknown): ApiError {
  const value = asRecord(raw, 'error')
  const kind = value.kind === 'rate_limited' ? 'rate_limited' : 'server'
  const retryAfter =
    typeof value.retry_after === 'number' && value.retry_after > 0 ? value.retry_after : undefined
  return new ApiError('The answer could not be completed.', 200, kind, retryAfter)
}

function parsed(data: string, context: string): unknown {
  try {
    return JSON.parse(data)
  } catch {
    throw new InvalidResponseError(context)
  }
}

/**
 * Asks a question and receives the answer as it is generated.
 *
 * The server only ever sends text it has already verified - nothing before the
 * first valid citation - and ends with `done`, the authoritative answer, which
 * REPLACES what was shown. That last step is what keeps the rare withdrawn
 * answer honest: the final state is always the server's verdict, never the
 * sum of the pieces.
 */
export async function streamQuestion(
  question: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<AnswerPayload> {
  // The mock backend has no stream: the answer arrives in one piece, which
  // is also what a one-sentence answer looks like in production.
  if (API_MODE === 'mock') return mockBackend.ask(question)

  const response = await openStream('/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
    signal,
    // Above the backend's own limit, for the same reason as askQuestion.
    timeoutMs: 60_000,
  })
  const body = response.body
  if (!body) throw new ApiError('The response has no body.', 0, 'network')

  let text = ''
  for await (const { event, data } of readServerSentEvents(body)) {
    if (event === 'stage') {
      handlers.onGenerating()
    } else if (event === 'token') {
      text += asString(asRecord(parsed(data, 'token'), 'token').text, 'token.text')
      handlers.onText(text)
    } else if (event === 'done') {
      return toAnswerPayload(parsed(data, 'done'))
    } else if (event === 'error') {
      throw streamErrorOf(parsed(data, 'error'))
    }
    // Unknown events are ignored: the server may add one before this client
    // is updated, and that must not break an answer in progress.
  }
  // The connection closed without a verdict: a proxy cut it, or the network
  // dropped. Whatever was shown is not an answer.
  throw new ApiError('The answer was interrupted.', 0, 'network')
}

/**
 * Asks a question. A missing answer ("insufficient_evidence") is a normal
 * outcome returned by the backend, NOT an exception.
 */
export async function askQuestion(question: string, signal?: AbortSignal): Promise<AnswerPayload> {
  if (API_MODE === 'mock') return mockBackend.ask(question)
  const raw = await request<unknown>('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
    signal,
    // Generous on purpose, and deliberately ABOVE the thirty seconds the
    // backend gives the provider: if the browser gave up first it would cancel
    // answers the server was about to deliver, and the user would pay a
    // question of quota for nothing.
    timeoutMs: 60_000,
  })
  return toAnswerPayload(raw)
}
