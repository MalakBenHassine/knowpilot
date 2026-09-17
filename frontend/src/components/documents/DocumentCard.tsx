import { Check, FileText, RotateCcw, Trash2, TriangleAlert } from 'lucide-react'
import { formatBytes, formatKind, formatRelativeTime } from '../../lib/format'
import type { DocumentFailureReason, StoredDocument } from '../../types/document'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { ProgressBar } from '../ui/ProgressBar'
import { Spinner } from '../ui/Spinner'

/** User-facing explanations. Backend internals are never shown. */
const FAILURE_MESSAGES: Record<DocumentFailureReason, string> = {
  no_text_found: 'No readable text was found. Scanned documents are not supported yet.',
  unsupported_format: 'This file format cannot be processed.',
  too_large: 'This file is too large to process.',
  processing_error: 'Unable to process this document.',
}

export function DocumentCard({
  document,
  onRetry,
  onRemove,
}: {
  document: StoredDocument
  onRetry: (id: string) => void
  onRemove: (id: string) => void
}) {
  const { status } = document

  return (
    <Card className="p-4 animate-rise">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-md bg-surface-sunken text-ink-subtle">
          <FileText size={17} />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <p className="min-w-0 truncate font-medium text-ink">{document.filename}</p>
            <StatusBadge status={status} />
          </div>

          <p className="mt-0.5 text-caption text-ink-subtle">
            {[
              formatKind(document.mimeType),
              formatBytes(document.sizeBytes),
              document.pageCount ? `${document.pageCount} pages` : null,
              formatRelativeTime(document.createdAt),
            ]
              .filter(Boolean)
              .join(' · ')}
          </p>

          <div className="mt-3">
            {status === 'uploading' || status === 'processing' ? (
              <div className="space-y-2">
                <p className="flex items-center gap-2 text-caption text-ink-muted">
                  <Spinner size={12} />
                  {status === 'uploading' ? 'Uploading file…' : 'Indexing document…'}
                </p>
                <ProgressBar
                  value={document.progress}
                  label={status === 'uploading' ? 'Upload progress' : 'Indexing progress'}
                />
                {status === 'processing' ? (
                  <p className="text-caption text-ink-subtle">
                    Extracting text, splitting it into passages and building the search index.
                  </p>
                ) : null}
              </div>
            ) : null}

            {status === 'ready' ? (
              <p className="text-caption text-ink-muted">
                Indexed successfully
                {document.chunkCount ? ` · ${document.chunkCount} passages searchable` : ''}
              </p>
            ) : null}

            {status === 'failed' ? (
              <div className="space-y-3">
                <p className="text-caption text-danger">
                  {FAILURE_MESSAGES[document.failureReason ?? 'processing_error']}
                </p>
                <div className="flex gap-2">
                  <Button size="sm" variant="secondary" onClick={() => onRetry(document.id)}>
                    <RotateCcw size={13} />
                    Try again
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => onRemove(document.id)}>
                    <Trash2 size={13} />
                    Remove
                  </Button>
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {status === 'ready' ? (
          <button
            type="button"
            onClick={() => onRemove(document.id)}
            aria-label={`Remove ${document.filename}`}
            className="rounded-sm p-2 text-ink-subtle opacity-0 transition-opacity duration-150 hover:bg-surface-sunken hover:text-danger focus-visible:opacity-100 group-hover:opacity-100 sm:opacity-100"
          >
            <Trash2 size={15} />
          </button>
        ) : null}
      </div>
    </Card>
  )
}

function StatusBadge({ status }: { status: StoredDocument['status'] }) {
  if (status === 'ready') {
    return (
      <Badge tone="success" icon={<Check size={12} />}>
        Ready
      </Badge>
    )
  }
  if (status === 'failed') {
    return (
      <Badge tone="error" icon={<TriangleAlert size={12} />}>
        Failed
      </Badge>
    )
  }
  return (
    <Badge tone="processing" icon={<Spinner size={10} />}>
      {status === 'uploading' ? 'Uploading' : 'Indexing'}
    </Badge>
  )
}
