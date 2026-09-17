import { AlertTriangle, CircleAlert, Info } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'

type Tone = 'info' | 'warning' | 'error'

const TONES: Record<Tone, { box: string; icon: ReactNode }> = {
  info: {
    box: 'bg-accent-soft/60 border-accent-line text-ink',
    icon: <Info size={16} className="text-accent" />,
  },
  warning: {
    box: 'bg-warning-soft border-transparent text-ink',
    icon: <AlertTriangle size={16} className="text-warning" />,
  },
  error: {
    box: 'bg-danger-soft border-transparent text-ink',
    icon: <CircleAlert size={16} className="text-danger" />,
  },
}

export function Alert({
  tone = 'info',
  title,
  children,
  action,
  className,
}: {
  tone?: Tone
  title: string
  children?: ReactNode
  action?: ReactNode
  className?: string
}) {
  const { box, icon } = TONES[tone]
  return (
    <div
      // Errors are announced immediately; informational alerts wait for a pause.
      role={tone === 'error' ? 'alert' : 'status'}
      className={cn('flex gap-3 rounded-md border p-4 animate-fade-in', box, className)}
    >
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        <p className="font-medium">{title}</p>
        {children ? <div className="mt-1 text-ink-muted">{children}</div> : null}
        {action ? <div className="mt-3">{action}</div> : null}
      </div>
    </div>
  )
}
