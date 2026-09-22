import { Clock, RotateCcw, SearchX, TriangleAlert } from 'lucide-react'
import Markdown from 'react-markdown'
import { formatWait } from '../../lib/format'
import type { AssistantTurn, ChatErrorKind } from '../../types/chat'
import type { Source } from '../../types/source'
import { Alert } from '../ui/Alert'
import { Button } from '../ui/Button'
import { Logo } from '../ui/Logo'
import { LoadingAnswer } from './LoadingAnswer'
import { SourceCard } from './SourceCard'

const ERROR_COPY: Record<ChatErrorKind, string> = {
  network: 'We could not reach the server. Check your connection and try again.',
  timeout: 'The answer took too long to arrive. Please try again.',
  rate_limited: 'Too many questions in a short time. Wait a moment and try again.',
  busy: 'The assistant is handling too many requests right now. Try again in a moment.',
  server: 'Something went wrong on our side. Please try again.',
}

/**
 * A refusal that says WHEN to come back.
 *
 * The backend sends Retry-After precisely so the interface does not have to
 * guess, and "try again in about 3 hours" is something a person can act on -
 * "try again later" is something they can only re-attempt and be refused for.
 */
/** "rétroviseur" or "rétroviseur" and "code PIN", in typographic quotes. */
function quoted(subjects: string[]): string {
  const each = subjects.map((subject) => `“${subject}”`)
  return each.length > 1 ? `${each.slice(0, -1).join(', ')} and ${each.at(-1)}` : (each[0] ?? '')
}

function errorCopy(kind: ChatErrorKind, retryAfterSeconds?: number): string {
  if (kind === 'rate_limited' && retryAfterSeconds !== undefined) {
    return `You have reached a limit on questions. Try again in ${formatWait(retryAfterSeconds)}.`
  }
  if (kind === 'busy' && retryAfterSeconds !== undefined) {
    // Not "your" anything: the user did nothing wrong, and was not charged.
    return `The assistant is handling too many requests right now. Try again in ${formatWait(retryAfterSeconds)}. This question was not counted.`
  }
  return ERROR_COPY[kind]
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
      {/* aria-busy while text is still arriving: without it a screen reader
          would announce every fragment, dozens of times per answer. It reads
          the answer once, when it has settled. */}
      <div
        className="min-w-0 flex-1"
        aria-live="polite"
        aria-atomic="false"
        aria-busy={turn.state.phase === 'loading' || turn.state.phase === 'streaming'}
      >
        {turn.state.phase === 'loading' ? <LoadingAnswer stage={turn.state.stage} /> : null}

        {turn.state.phase === 'streaming' ? (
          <div className="prose-answer text-body text-ink">
            {/* Only verified text reaches this state: the server holds
                everything back until the first valid citation. */}
            <Markdown>{turn.state.text}</Markdown>
            <span
              className="ml-0.5 inline-block h-4 w-1.5 animate-pulse-soft rounded-sm bg-accent align-text-bottom"
              aria-hidden="true"
            />
          </div>
        ) : null}

        {turn.state.phase === 'answered' ? (
          <div className="space-y-4">
            {/* Above the answer, not below it: it changes how the answer
                should be read, so it is read first. A warning tone rather
                than an error: the answer may well be right - a windscreen is
                glass - but it rests on a category, not on a name. */}
            {turn.state.notInDocuments.length > 0 ? (
              <Alert
                tone="warning"
                title={`Your documents never mention ${quoted(turn.state.notInDocuments)}.`}
              >
                This answer relies on what they say about something broader. Check that it
                applies before relying on it.
              </Alert>
            ) : null}

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
          // A budget that is spent is not a malfunction, so it does not get the
          // alarming colour: the neutral surface says "come back later", the
          // danger surface says "something is broken". Conflating the two
          // teaches people to ignore both.
          <div
            className={
              turn.state.kind === 'rate_limited' || turn.state.kind === 'busy'
                ? 'rounded-lg border border-line bg-surface-sunken p-4'
                : 'rounded-lg border border-transparent bg-danger-soft p-4'
            }
            role="alert"
          >
            <p className="flex items-center gap-2 font-medium text-ink">
              {turn.state.kind === 'rate_limited' ? (
                <>
                  <Clock size={16} className="text-ink-muted" />
                  You have reached your limit.
                </>
              ) : turn.state.kind === 'busy' ? (
                <>
                  <Clock size={16} className="text-ink-muted" />
                  The assistant is busy.
                </>
              ) : (
                <>
                  <TriangleAlert size={16} className="text-danger" />
                  Something went wrong.
                </>
              )}
            </p>
            <p className="mt-1.5 text-body text-ink-muted">
              {errorCopy(turn.state.kind, turn.state.retryAfterSeconds)}
            </p>
            {/* No Retry button on a spent budget: the only thing it can produce
                is the same refusal, and a button that cannot work is worse than
                no button. */}
            {turn.state.kind === 'rate_limited' ? null : (
              <Button
                size="sm"
                variant="secondary"
                className="mt-3"
                onClick={() => onRetry(turn.id)}
              >
                <RotateCcw size={13} />
                Retry
              </Button>
            )}
          </div>
        ) : null}
      </div>
    </div>
  )
}
