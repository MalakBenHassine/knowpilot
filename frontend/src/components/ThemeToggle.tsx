import { Monitor, Moon, Sun } from 'lucide-react'
import { useEffect, useState } from 'react'
import { cn } from '../lib/cn'
import { applyThemePreference, readThemePreference, type ThemePreference } from '../lib/theme'

const OPTIONS: Array<{ value: ThemePreference; label: string; icon: typeof Sun }> = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
]

export function ThemeToggle() {
  const [preference, setPreference] = useState<ThemePreference>(() => readThemePreference())

  useEffect(() => {
    applyThemePreference(preference)
  }, [preference])

  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      className="inline-flex rounded-md border border-line bg-surface p-0.5"
    >
      {OPTIONS.map(({ value, label, icon: Icon }) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={preference === value}
          aria-label={label}
          onClick={() => setPreference(value)}
          className={cn(
            'flex size-7 items-center justify-center rounded-sm transition-colors duration-150',
            preference === value
              ? 'bg-surface-sunken text-ink'
              : 'text-ink-subtle hover:text-ink',
          )}
        >
          <Icon size={14} />
        </button>
      ))}
    </div>
  )
}
