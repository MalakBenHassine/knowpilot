import { RotateCcw, SearchX, TriangleAlert } from 'lucide-react'
import Markdown from 'react-markdown'
import type { AssistantTurn, ChatErrorKind } from '../../types/chat'
import type { Source } from '../../types/source'
import { Button } from '../ui/Button'
import { Logo } from '../ui/Logo'
import { LoadingAnswer } from './LoadingAnswer'
import { SourceCard } from './SourceCard'

const ERROR_COPY: Record<ChatErrorKind, string> = {
  network: 'We could not reach the server. Check your connection and try again.',
  timeout: 'The answer took too long to arrive. Please try again.',
  rate_limited: 'Too many questions in a short time. Wait a moment and try again.',
  server: 'Something went wrong on our side. Please try again.',
}

export function AssistantMessage({
  turn,
  onRetry,
  onOpenSource,
}: {
  turn: AssistantTurn
  onRetry: (id: string) => void
  onOpenSource?: (source: Source) => void
}) {
  return (
    <div className="flex gap-3 animate-rise">
      <div className="mt-1 flex size-7 shrink-0 items-center justify-center rounded-md border border-line bg-surface">
        <Logo size={15} />
      </div>

      {/* Assistant output is announced politely once it settles. */}
      <div className="min-w-0 flex-1" aria-live="polite" aria-atomic="false">
        {turn.state.phase === 'loading' ? <LoadingAnswer stage={turn.state.stage} /> : null}

        {turn.state.phase === 'answered' ? (
          <div className="space-y-4">
            <div className="prose-answer text-body text-ink">
              {/* react-markdown does NOT render raw HTML by default: no XSS from
                  model output, which is untrusted content. */}
              <Markdown>{turn.state.answer}</Markdown>
            </div>

            {turn.state.sources.length > 0 ? (
              <section aria-label="Sources used for this answer" className="space-y-2">
                <h3 className="text-caption font-medium tracking-wide text-ink-subtle uppercase">
                  Sources
                </h3>
                <div className="grid gap-2 sm:grid-cols-2">
                  {turn.state.sources.map((source) => (
                    <SourceCard key={source.id} source={source} onOpen={onOpenSource} />
                  ))}
                </div>
              </section>
            ) : null}
          </div>
        ) : null}

        {turn.state.phase === 'insufficient_evidence' ? (
          // Deliberately NOT an error style: this is a valid, honest outcome.
          <div className="rounded-lg border border-line bg-surface-sunken p-4">
            <p className="flex items-center gap-2 font-medium text-ink">
              <SearchX size={16} className="text-ink-muted" />
              I couldn’t find enough evidence in your documents.
            </p>
            <p className="mt-1.5 text-body text-ink-muted">
              Try uploading another document, or asking a more specific question.
            </p>
          </div>
        ) : null}

        {turn.state.phase === 'error' ? (
          <div className="rounded-lg border border-transparent bg-danger-soft p-4" role="alert">
            <p className="flex items-center gap-2 font-medium text-ink">
              <TriangleAlert size={16} className="text-danger" />
              Something went wrong.
            </p>
            <p className="mt-1.5 text-body text-ink-muted">{ERROR_COPY[turn.state.kind]}</p>
            <Button size="sm" variant="secondary" className="mt-3" onClick={() => onRetry(turn.id)}>
              <RotateCcw size={13} />
              Retry
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  )
}
