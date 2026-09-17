import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { DocumentCard } from './DocumentCard'
import type { StoredDocument } from '../../types/document'

function documentWith(overrides: Partial<StoredDocument> = {}): StoredDocument {
  return {
    id: 'document-1',
    filename: 'contract.pdf',
    mimeType: 'application/pdf',
    sizeBytes: 2411724,
    status: 'ready',
    createdAt: new Date().toISOString(),
    retryable: false,
    ...overrides,
  }
}

describe('DocumentCard', () => {
  it('shows the real pipeline stage while indexing', () => {
    render(
      <DocumentCard
        document={documentWith({ status: 'processing', stage: 'embedding' })}
        onRetry={vi.fn()}
        onRemove={vi.fn()}
      />,
    )
    expect(screen.getByText(/computing embeddings/i)).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
  })

  it('offers "Try again" only when the backend says the failure is retryable', () => {
    const { rerender } = render(
      <DocumentCard
        document={documentWith({
          status: 'failed',
          failureReason: 'no_text_found',
          retryable: false,
        })}
        onRetry={vi.fn()}
        onRemove={vi.fn()}
      />,
    )
    // A scanned PDF will fail again: proposing a retry would mislead the user.
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
    expect(screen.getByText(/no readable text/i)).toBeInTheDocument()

    rerender(
      <DocumentCard
        document={documentWith({
          status: 'failed',
          failureReason: 'processing_error',
          retryable: true,
        })}
        onRetry={vi.fn()}
        onRemove={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })

  it('never shows a technical failure code to the user', () => {
    render(
      <DocumentCard
        document={documentWith({ status: 'failed', failureReason: 'unsupported_format' })}
        onRetry={vi.fn()}
        onRemove={vi.fn()}
      />,
    )
    expect(screen.queryByText(/unsupported_format/)).toBeNull()
    expect(screen.getByText(/format cannot be processed/i)).toBeInTheDocument()
  })
})
