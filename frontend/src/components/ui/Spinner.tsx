import { cn } from '../../lib/cn'

/** Decorative by default: the surrounding element carries the accessible text. */
export function Spinner({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <span
      aria-hidden="true"
      style={{ width: size, height: size, borderWidth: Math.max(1.5, size / 9) }}
      className={cn(
        'inline-block shrink-0 animate-spin rounded-full',
        'border-current border-t-transparent opacity-70',
        className,
      )}
    />
  )
}
