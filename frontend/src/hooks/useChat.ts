import { useCallback, useRef, useState } from 'react'
import { ApiError } from '../services/api'
import { askQuestion } from '../services/chat'
import type { AssistantTurn, ChatErrorKind, ChatTurn, UserTurn } from '../types/chat'

/** Delay before the loading copy moves from "searching" to "generating". */
const GENERATING_STAGE_DELAY_MS = 1200

function errorKindOf(error: unknown): ChatErrorKind {
  return error instanceof ApiError ? error.kind : 'server'
}

/**
 * Owns the conversation. One question produces exactly one assistant turn,
 * which then moves through loading -> answered | insufficient_evidence | error.
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

      const stageTimer = window.setTimeout(() => {
        updateAssistantTurn(assistantTurnId, { phase: 'loading', stage: 'generating' })
      }, GENERATING_STAGE_DELAY_MS)

      try {
        const payload = await askQuestion(question, controller.signal)
        updateAssistantTurn(
          assistantTurnId,
          payload.outcome === 'answered'
            ? { phase: 'answered', answer: payload.answer, sources: payload.sources }
            : { phase: 'insufficient_evidence' },
        )
      } catch (error) {
        updateAssistantTurn(assistantTurnId, { phase: 'error', kind: errorKindOf(error) })
      } finally {
        window.clearTimeout(stageTimer)
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
