import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AssistantMessage } from './AssistantMessage'
import type { AssistantTurn, AssistantTurnState } from '../../types/chat'

function turnWith(state: AssistantTurnState): AssistantTurn {
  return {
    id: 'turn-1',
    role: 'assistant',
    question: 'What does the contract say about the termination period?',
    state,
    createdAt: '2026-09-17T18:12:04Z',
  }
}

/**
 * The four outcomes of a question must be distinguishable. These tests exist
 * so nobody collapses them back into a single isLoading boolean.
 */
describe('AssistantMessage', () => {
  it('shows what the system is doing while loading', () => {
    const { rerender } = render(
      <AssistantMessage turn={turnWith({ phase: 'loading', stage: 'retrieving' })} onRetry={vi.fn()} />,
    )
    expect(screen.getByText(/searching your documents/i)).toBeInTheDocument()

    rerender(
      <AssistantMessage turn={turnWith({ phase: 'loading', stage: 'generating' })} onRetry={vi.fn()} />,
    )
    expect(screen.getByText(/generating answer/i)).toBeInTheDocument()
  })

  it('renders the answer with its sources', () => {
    render(
      <AssistantMessage
        turn={turnWith({
          phase: 'answered',
          answer: 'The termination period is **30 days**.',
          sources: [
            {
              id: 'source-1',
              documentId: 'document-1',
              filename: 'contract.pdf',
              page: 3,
              snippet: 'The termination period is thirty (30) days.',
              score: 0.82,
            },
          ],
        })}
        onRetry={vi.fn()}
      />,
    )

    expect(screen.getByText(/30 days/)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: /sources/i })).toBeInTheDocument()
    expect(screen.getByText('contract.pdf')).toBeInTheDocument()
    expect(screen.getByText('p. 3')).toBeInTheDocument()
    expect(screen.getByText('0.82')).toBeInTheDocument()
  })

  it('does not render HTML coming from the model (XSS)', () => {
    render(
      <AssistantMessage
        turn={turnWith({
          phase: 'answered',
          answer: 'Harmless <img src=x onerror="alert(1)"> text',
          sources: [],
        })}
        onRetry={vi.fn()}
      />,
    )

    // Model output is untrusted: it is displayed as text, never as markup.
    expect(document.querySelector('img')).toBeNull()
  })

  it('presents "insufficient evidence" as an answer, not as an error', () => {
    render(<AssistantMessage turn={turnWith({ phase: 'insufficient_evidence' })} onRetry={vi.fn()} />)

    expect(screen.getByText(/couldn’t find enough evidence/i)).toBeInTheDocument()
    // No alert role and no retry: nothing is broken, so nothing to retry.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('announces a real error and offers a retry', () => {
    const onRetry = vi.fn()
    render(<AssistantMessage turn={turnWith({ phase: 'error', kind: 'timeout' })} onRetry={onRetry} />)

    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/took too long/i)).toBeInTheDocument()
    screen.getByRole('button', { name: /retry/i }).click()
    expect(onRetry).toHaveBeenCalledWith('turn-1')
  })

  it('uses a different message for each error kind', () => {
    const { rerender } = render(
      <AssistantMessage turn={turnWith({ phase: 'error', kind: 'network' })} onRetry={vi.fn()} />,
    )
    expect(screen.getByText(/could not reach the server/i)).toBeInTheDocument()

    rerender(<AssistantMessage turn={turnWith({ phase: 'error', kind: 'rate_limited' })} onRetry={vi.fn()} />)
    expect(screen.getByText(/too many questions/i)).toBeInTheDocument()
  })
})
