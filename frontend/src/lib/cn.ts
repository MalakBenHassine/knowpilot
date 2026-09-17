/** Join class names, skipping falsy values. Keeps conditional classes readable. */
export function cn(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(' ')
}
