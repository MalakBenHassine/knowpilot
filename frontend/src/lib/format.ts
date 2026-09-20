/** Human readable file size: 2411724 -> "2.3 MB". */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unitIndex]}`
}

/** "application/pdf" -> "PDF". Shown on document cards. */
export function formatKind(mimeType: string): string {
  if (mimeType === 'application/pdf') return 'PDF'
  if (mimeType.startsWith('text/')) return 'TXT'
  return 'FILE'
}

/**
 * A waiting time a person can act on: "a moment", "12 minutes", "3 hours".
 *
 * Rounded up, never down: telling somebody to come back in 2 hours when the
 * counter resets in 2 h 50 earns one more refusal. An estimate that errs
 * towards patience is kinder than one that errs towards a second rejection.
 */
export function formatWait(seconds: number): string {
  if (seconds < 60) return 'a moment'
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return `${minutes} minutes`
  const hours = Math.ceil(minutes / 60)
  return hours === 1 ? 'an hour' : `${hours} hours`
}

/** Short relative time: "just now", "5 min ago", "3 d ago". */
export function formatRelativeTime(isoDate: string): string {
  const elapsedMs = Date.now() - new Date(isoDate).getTime()
  const minutes = Math.floor(elapsedMs / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  return `${Math.floor(hours / 24)} d ago`
}
