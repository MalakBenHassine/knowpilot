import type { LoadingStage } from '../../types/chat'

const STAGE_LABEL: Record<LoadingStage, string> = {
  retrieving: 'Searching your documents…',
  generating: 'Generating answer…',
}

/**
 * Shows WHAT the system is doing, not just "loading". The two stages mirror the
 * real pipeline: retrieval first, generation second.
 */
export function LoadingAnswer({ stage }: { stage: LoadingStage }) {
  return (
    <div className="space-y-3" aria-live="polite">
      <p className="flex items-center gap-2 text-body text-ink-muted">
        <span className="relative flex size-2">
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-accent opacity-60" />
          <span className="relative inline-flex size-2 rounded-full bg-accent" />
        </span>
        {STAGE_LABEL[stage]}
      </p>

      {/* Skeleton lines: they hint at the shape of the answer to come. */}
      <div className="space-y-2" aria-hidden="true">
        <div className="h-3 w-4/5 rounded-sm bg-surface-sunken animate-pulse-soft" />
        <div className="h-3 w-3/5 rounded-sm bg-surface-sunken animate-pulse-soft" />
        {stage === 'generating' ? (
          <div className="h-3 w-2/5 rounded-sm bg-surface-sunken animate-pulse-soft" />
        ) : null}
      </div>
    </div>
  )
}
