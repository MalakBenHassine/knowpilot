import { ArrowUp } from 'lucide-react'
import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { cn } from '../../lib/cn'
import { Spinner } from '../ui/Spinner'

const MAX_LENGTH = 2000

export function QuestionInput({
  onSubmit,
  isBusy,
  disabledReason,
}: {
  onSubmit: (question: string) => void
  isBusy: boolean
  /** When set, the composer is disabled and this explains why. */
  disabledReason?: string
}) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const isDisabled = Boolean(disabledReason)
  const canSubmit = value.trim().length > 0 && !isBusy && !isDisabled

  // The textarea grows with the content, up to a readable maximum.
  useEffect(() => {
    const element = textareaRef.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, 160)}px`
  }, [value])

  function submit() {
    if (!canSubmit) return
    onSubmit(value)
    setValue('')
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter adds a line: the convention users expect.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  return (
    <div className="border-t border-line bg-bg/85 px-4 py-3 backdrop-blur">
      <div className="mx-auto max-w-3xl">
        <div
          className={cn(
            'flex items-end gap-2 rounded-lg border bg-surface p-2 shadow-xs transition-colors duration-150',
            isDisabled ? 'border-line opacity-70' : 'border-line focus-within:border-accent-line',
          )}
        >
          <label htmlFor="question" className="sr-only">
            Ask a question about your documents
          </label>
          <textarea
            id="question"
            ref={textareaRef}
            rows={1}
            value={value}
            maxLength={MAX_LENGTH}
            disabled={isDisabled}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={disabledReason ?? 'Ask something about your documents…'}
            aria-describedby="composer-hint"
            className="max-h-40 min-h-9 w-full resize-none bg-transparent px-2 py-1.5 text-body text-ink outline-none placeholder:text-ink-subtle disabled:cursor-not-allowed"
          />
          <button
            type="button"
            onClick={submit}
            disabled={!canSubmit}
            aria-label={isBusy ? 'Waiting for the current answer' : 'Send question'}
            className={cn(
              'flex size-9 shrink-0 items-center justify-center rounded-md transition-[background-color,opacity,transform] duration-150',
              canSubmit
                ? 'bg-accent text-white hover:bg-accent-hover active:scale-95'
                : 'bg-surface-sunken text-ink-subtle',
            )}
          >
            {isBusy ? <Spinner size={14} /> : <ArrowUp size={16} />}
          </button>
        </div>

        <p id="composer-hint" className="mt-2 px-1 text-caption text-ink-subtle">
          {isBusy
            ? 'Waiting for the current answer…'
            : 'Enter to send · Shift + Enter for a new line · answers are grounded in your documents'}
        </p>
      </div>
    </div>
  )
}
