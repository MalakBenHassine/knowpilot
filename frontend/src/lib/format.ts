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
