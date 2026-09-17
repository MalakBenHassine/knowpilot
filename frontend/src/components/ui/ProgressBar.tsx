/** Determinate progress when the backend reports it, indeterminate otherwise. */
export function ProgressBar({ value, label }: { value?: number; label: string }) {
  const isDeterminate = typeof value === 'number'
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuenow={isDeterminate ? Math.round(value) : undefined}
      aria-valuemin={0}
      aria-valuemax={100}
      className="h-1 w-full overflow-hidden rounded-full bg-surface-sunken"
    >
      <div
        className={
          isDeterminate
            ? 'h-full rounded-full bg-accent transition-[width] duration-500 ease-out'
            : 'h-full w-1/3 rounded-full bg-accent animate-pulse-soft'
        }
        style={isDeterminate ? { width: `${Math.min(100, Math.max(0, value))}%` } : undefined}
      />
    </div>
  )
}
