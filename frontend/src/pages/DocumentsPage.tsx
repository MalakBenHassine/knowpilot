import { FileText } from 'lucide-react'
import { DocumentCard } from '../components/documents/DocumentCard'
import { UploadZone } from '../components/documents/UploadZone'
import { Alert } from '../components/ui/Alert'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'
import { useDocumentsContext } from '../hooks/documents-context'

export function DocumentsPage() {
  const { documents, isLoading, loadError, uploadError, upload, remove, retry, refresh } =
    useDocumentsContext()

  const indexingCount = documents.filter(
    (document) => document.status === 'processing' || document.status === 'uploading',
  ).length

  return (
    <div className="mx-auto max-w-3xl px-4 py-8 sm:px-6">
      <header>
        <h1 className="text-display font-semibold text-ink">Your documents</h1>
        <p className="mt-1.5 text-body text-ink-muted">
          Manage the knowledge available to KnowPilot.
        </p>
      </header>

      <div className="mt-6">
        <UploadZone onFiles={(files) => files.forEach((file) => void upload(file))} />
      </div>

      {uploadError ? (
        <Alert tone="error" title="Upload failed" className="mt-4">
          {uploadError}
        </Alert>
      ) : null}

      <section className="mt-8" aria-label="Uploaded documents">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-heading font-semibold text-ink">
            Library
            {documents.length > 0 ? (
              <span className="ml-2 text-body font-normal text-ink-subtle">{documents.length}</span>
            ) : null}
          </h2>
          {/* Status of the background work, announced to assistive tech. */}
          <p aria-live="polite" className="text-caption text-ink-subtle">
            {indexingCount > 0 ? `${indexingCount} document(s) being indexed…` : null}
          </p>
        </div>

        {isLoading ? (
          <ul className="space-y-3" aria-hidden="true">
            {[0, 1].map((key) => (
              <li key={key} className="h-24 rounded-lg border border-line bg-surface animate-pulse-soft" />
            ))}
          </ul>
        ) : null}

        {!isLoading && loadError ? (
          <Alert
            tone="error"
            title="We could not load your documents."
            action={
              <Button size="sm" variant="secondary" onClick={() => void refresh()}>
                Try again
              </Button>
            }
          >
            Check your connection and try again.
          </Alert>
        ) : null}

        {!isLoading && !loadError && documents.length === 0 ? (
          <EmptyState
            icon={<FileText size={20} />}
            title="No documents yet"
            description="Upload your first document to start asking questions."
          />
        ) : null}

        {documents.length > 0 ? (
          <ul className="space-y-3">
            {documents.map((document) => (
              <li key={document.id}>
                <DocumentCard
                  document={document}
                  onRetry={(id) => void retry(id)}
                  onRemove={(id) => void remove(id)}
                />
              </li>
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  )
}
