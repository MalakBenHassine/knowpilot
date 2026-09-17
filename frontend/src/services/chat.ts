import type { AnswerPayload } from '../types/chat'
import { API_MODE, request } from './api'
import { mockBackend } from './mockBackend'

/**
 * Asks a question. A missing answer ("insufficient_evidence") is a normal
 * outcome returned by the backend, NOT an exception.
 */
export async function askQuestion(question: string, signal?: AbortSignal): Promise<AnswerPayload> {
  if (API_MODE === 'mock') return mockBackend.ask(question)
  return request<AnswerPayload>('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
    signal,
    timeoutMs: 60_000,
  })
}
