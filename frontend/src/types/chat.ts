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
 * Distinct states — never a single isLoading boolean.
 */
export type AssistantTurnState =
  | { phase: 'loading'; stage: LoadingStage }
  /**
   * Verified text arriving. Deliberately without sources: they belong to the
   * final answer, and a source card shown before the verdict could be for a
   * passage the final answer does not cite.
   */
  | { phase: 'streaming'; text: string }
  | { phase: 'answered'; answer: string; sources: Source[] }
  | { phase: 'insufficient_evidence' }
  | {
      phase: 'error'
      kind: ChatErrorKind
      /**
       * Present only when the server told us when to come back (a 429 with a
       * Retry-After header). Optional rather than defaulted: "we were not
       * told" and "come back in zero seconds" are different facts, and the
       * copy must be able to tell them apart.
       */
      retryAfterSeconds?: number
    }

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
