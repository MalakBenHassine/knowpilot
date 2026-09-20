import type { AnswerPayload } from '../types/chat'
import type { Source } from '../types/source'
import { API_MODE, request } from './api'
import { mockBackend } from './mockBackend'
import { asBoolean, asNumber, asRecord, asString, InvalidResponseError } from './parse'

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
