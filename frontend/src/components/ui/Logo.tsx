import { cn } from '../../lib/cn'

/**
 * KnowPilot mark: a document page with a retrieval "beam" — the product in one
 * glyph (a source, and the line that points to it).
 */
export function Logo({ size = 24, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      role="img"
      aria-label="KnowPilot"
      className={cn('shrink-0', className)}
    >
      <rect x="3" y="2.5" width="13" height="19" rx="3" className="fill-accent-soft" />
      <rect
        x="3"
        y="2.5"
        width="13"
        height="19"
        rx="3"
        className="stroke-accent"
        strokeWidth="1.4"
      />
      <path d="M6.5 8h6M6.5 11.5h6M6.5 15h3.5" className="stroke-accent" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M14 18.5 21 11.5" className="stroke-ink" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="21" cy="11.5" r="1.8" className="fill-ink" />
    </svg>
  )
}
