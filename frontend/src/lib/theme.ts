export type ThemePreference = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'knowpilot.theme'

/** Reads the stored preference; falls back to "system" when unavailable. */
export function readThemePreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') return stored
  } catch {
    // Private mode or blocked storage: the default is good enough.
  }
  return 'system'
}

/** Applies the preference to <html> and remembers it. */
export function applyThemePreference(preference: ThemePreference): void {
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
  const isDark = preference === 'dark' || (preference === 'system' && prefersDark)
  document.documentElement.classList.toggle('dark', isDark)
  try {
    localStorage.setItem(STORAGE_KEY, preference)
  } catch {
    // Not critical: the theme still applies for this page view.
  }
}
