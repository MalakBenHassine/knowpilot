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

  it('shows streamed text without sources until the verdict arrives', () => {
    const { container } = render(
      <AssistantMessage
        turn={turnWith({ phase: 'streaming', text: 'The termination period is **30 days** [1].' })}
        onRetry={vi.fn()}
      />,
    )

    expect(screen.getByText('30 days')).toBeInTheDocument()
    // Sources belong to the final answer only.
    expect(screen.queryByRole('region', { name: /sources/i })).not.toBeInTheDocument()
    // Screen readers wait for the settled answer instead of every fragment.
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  })

  it('labels each source with the number the answer cites it by', () => {
    render(
      <AssistantMessage
        turn={turnWith({
          phase: 'answered',
          answer: 'Franchise de 90 euros [2].',
          sources: [
            {
              id: 'doc#2',
              number: 2,
              documentId: 'doc',
              filename: 'contrat.pdf',
              page: 2,
              snippet: '90 euros si remplacement.',
            },
          ],
          notInDocuments: [],
        })}
        onRetry={vi.fn()}
      />,
    )

    // [2] in the text, [2] on the card: no matching by guesswork.
    expect(screen.getByLabelText('Source 2')).toHaveTextContent('[2]')
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
          notInDocuments: [],
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

  it('says so when the answer rests on something the documents never name', () => {
    render(
      <AssistantMessage
        turn={turnWith({
          phase: 'answered',
          answer: 'Bris de glace : 90 euros si remplacement [1].',
          sources: [],
          notInDocuments: ['rétroviseur', 'code PIN'],
        })}
        onRetry={vi.fn()}
      />,
    )

    // A status, not an alert: the answer may be right, nothing is broken.
    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('Your documents never mention “rétroviseur” and “code PIN”.')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows no notice when the documents name what was asked', () => {
    render(
      <AssistantMessage
        turn={turnWith({
          phase: 'answered',
          answer: 'Vol : 350 euros [1].',
          sources: [],
          notInDocuments: [],
        })}
        onRetry={vi.fn()}
      />,
    )

    expect(screen.queryByText(/never mention/i)).toBeNull()
  })

  it('does not render HTML coming from the model (XSS)', () => {
    render(
      <AssistantMessage
        turn={turnWith({
          phase: 'answered',
          answer: 'Harmless <img src=x onerror="alert(1)"> text',
          sources: [],
          notInDocuments: [],
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

  it('never tells a user they used their questions when the service is busy', () => {
    render(
      <AssistantMessage
        turn={turnWith({ phase: 'error', kind: 'busy', retryAfterSeconds: 120 })}
        onRetry={vi.fn()}
      />,
    )

    expect(screen.getByText('The assistant is busy.')).toBeInTheDocument()
    expect(screen.getByText(/not counted/i)).toBeInTheDocument()
    expect(screen.queryByText(/your limit|used your questions/i)).toBeNull()
    // Waiting fixes it, so the question can be asked again from here.
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })
})
