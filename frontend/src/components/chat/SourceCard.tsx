import { ArrowUpRight, FileText } from 'lucide-react'
import type { Source } from '../../types/source'
import { Card } from '../ui/Card'

/**
 * Every value shown here comes from the retrieval metadata returned by the API.
 * When the backend does not send a page or a score, nothing is displayed —
 * the UI never fabricates provenance.
 */
export function SourceCard({ source, onOpen }: { source: Source; onOpen?: (source: Source) => void }) {
  return (
    <Card interactive={Boolean(onOpen)} className="group p-3">
      <button
        type="button"
        onClick={() => onOpen?.(source)}
        className="flex w-full flex-col items-start gap-2 text-left"
      >
        <div className="flex w-full items-center gap-2">
          <FileText size={14} className="shrink-0 text-ink-subtle" />
          <span className="min-w-0 truncate text-caption font-medium text-ink">
            {source.filename}
          </span>
          {typeof source.page === 'number' ? (
            <span className="shrink-0 text-caption text-ink-subtle">p. {source.page}</span>
          ) : null}
          {typeof source.score === 'number' ? (
            <span
              className="ml-auto shrink-0 rounded-sm bg-surface-sunken px-1.5 text-caption text-ink-subtle"
              title="Retrieval similarity score"
            >
              {source.score.toFixed(2)}
            </span>
          ) : null}
        </div>

        <p className="line-clamp-3 text-caption text-ink-muted">“{source.snippet}”</p>

        {onOpen ? (
          <span className="inline-flex items-center gap-1 text-caption font-medium text-accent opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100">
            View source
            <ArrowUpRight size={12} />
          </span>
        ) : null}
      </button>
    </Card>
  )
}
