import type { ReactNode } from 'react'

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-line px-6 py-16 text-center animate-fade-in">
      <div className="mb-4 flex size-11 items-center justify-center rounded-md bg-surface-sunken text-ink-subtle">
        {icon}
      </div>
      <h2 className="text-heading font-semibold text-ink">{title}</h2>
      <p className="mt-1 max-w-sm text-body text-ink-muted">{description}</p>
      {action ? <div className="mt-6">{action}</div> : null}
    </div>
  )
}
