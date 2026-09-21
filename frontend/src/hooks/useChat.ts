import { useCallback, useRef, useState } from 'react'
import { ApiError } from '../services/api'
import { streamQuestion } from '../services/chat'
import type { AssistantTurn, AssistantTurnState, ChatTurn, UserTurn } from '../types/chat'

/**
 * Anything that is not an ApiError is a bug in our own code, and a bug is a
 * server-class failure from the point of view of somebody waiting for an
 * answer: the honest thing to show is "something went wrong", not a guess.
 */
function errorStateOf(error: unknown): AssistantTurnState {
  if (!(error instanceof ApiError)) return { phase: 'error', kind: 'server' }
  return { phase: 'error', kind: error.kind, retryAfterSeconds: error.retryAfterSeconds }
}

/**
 * Owns the conversation. One question produces exactly one assistant turn,
 * which then moves through
 *   loading -> [streaming ->] answered | insufficient_evidence | error.
 */
export function useChat() {
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [isBusy, setIsBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  const updateAssistantTurn = useCallback((id: string, state: AssistantTurn['state']) => {
    setTurns((previous) =>
      previous.map((turn) =>
        turn.id === id && turn.role === 'assistant' ? { ...turn, state } : turn,
      ),
    )
  }, [])

  const run = useCallback(
    async (question: string, assistantTurnId: string) => {
      setIsBusy(true)
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      try {
        const payload = await streamQuestion(
          question,
          {
            // A real stage, sent by the server when retrieval is over - it used
            // to be guessed with a 1.2 s timer, which was wrong both ways.
            onGenerating: () =>
              updateAssistantTurn(assistantTurnId, { phase: 'loading', stage: 'generating' }),
            onText: (text) => updateAssistantTurn(assistantTurnId, { phase: 'streaming', text }),
          },
          controller.signal,
        )
        // The final state is the server's verdict, never the streamed text:
        // it carries the sources, and it replaces the text if the answer was
        // withdrawn at the last moment.
        updateAssistantTurn(
          assistantTurnId,
          payload.outcome === 'answered'
            ? {
                phase: 'answered',
                answer: payload.answer,
                sources: payload.sources,
                notInDocuments: payload.notInDocuments,
              }
            : { phase: 'insufficient_evidence' },
        )
      } catch (error) {
        updateAssistantTurn(assistantTurnId, errorStateOf(error))
      } finally {
        setIsBusy(false)
      }
    },
    [updateAssistantTurn],
  )

  const ask = useCallback(
    async (rawQuestion: string) => {
      const question = rawQuestion.trim()
      // Guard against double submit: the composer is disabled, this is the net.
      if (!question || isBusy) return

      const now = new Date().toISOString()
      const userTurn: UserTurn = { id: crypto.randomUUID(), role: 'user', question, createdAt: now }
      const assistantTurn: AssistantTurn = {
        id: crypto.randomUUID(),
        role: 'assistant',
        question,
        state: { phase: 'loading', stage: 'retrieving' },
        createdAt: now,
      }
      setTurns((previous) => [...previous, userTurn, assistantTurn])
      await run(question, assistantTurn.id)
    },
    [isBusy, run],
  )

  const retry = useCallback(
    async (assistantTurnId: string) => {
      const turn = turns.find((candidate) => candidate.id === assistantTurnId)
      if (!turn || turn.role !== 'assistant' || isBusy) return
      updateAssistantTurn(assistantTurnId, { phase: 'loading', stage: 'retrieving' })
      await run(turn.question, assistantTurnId)
    },
    [isBusy, run, turns, updateAssistantTurn],
  )

  return { turns, isBusy, ask, retry }
}
