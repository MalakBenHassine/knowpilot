import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'

type Tone = 'neutral' | 'success' | 'processing' | 'error'

const TONES: Record<Tone, string> = {
  neutral: 'bg-surface-sunken text-ink-muted border-line',
  success: 'bg-success-soft text-success border-transparent',
  processing: 'bg-accent-soft text-accent border-transparent',
  error: 'bg-danger-soft text-danger border-transparent',
}

export function Badge({
  tone = 'neutral',
  icon,
  children,
}: {
  tone?: Tone
  icon?: ReactNode
  children: ReactNode
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5',
        'text-caption font-medium whitespace-nowrap',
        TONES[tone],
      )}
    >
      {icon}
      {children}
    </span>
  )
}
