import type { Source } from './source'

/**
 * The backend answers with one of these outcomes.
 * "insufficient_evidence" is a SUCCESSFUL response, not an error: retrieval
 * found nothing relevant enough, so no answer was generated.
 */
export type AnswerPayload =
  | { outcome: 'answered'; answer: string; sources: Source[] }
  | { outcome: 'insufficient_evidence' }

/** What the assistant is doing while we wait. Drives the loading copy. */
export type LoadingStage = 'retrieving' | 'generating'

/** Why a request failed. Used to pick the user-facing message. */
export type ChatErrorKind = 'network' | 'timeout' | 'rate_limited' | 'server'

/**
 * Client-side state of one assistant turn.
 * Four distinct states — never a single isLoading boolean.
 */
export type AssistantTurnState =
  | { phase: 'loading'; stage: LoadingStage }
  | { phase: 'answered'; answer: string; sources: Source[] }
  | { phase: 'insufficient_evidence' }
  | { phase: 'error'; kind: ChatErrorKind }

export interface UserTurn {
  id: string
  role: 'user'
  question: string
  createdAt: string
}

export interface AssistantTurn {
  id: string
  role: 'assistant'
  /** The question this turn answers, so it can be retried. */
  question: string
  state: AssistantTurnState
  createdAt: string
}

export type ChatTurn = UserTurn | AssistantTurn
