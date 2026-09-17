import { ApiError } from './api'

/**
 * Runtime validation of API responses.
 *
 * TypeScript types are erased at compile time: `request<Document[]>` is a
 * promise made to the compiler, not a check. A proxy error page, an older
 * deployed backend or a null field would all pass the type checker and crash
 * the UI later, far from the cause. So the boundary validates what it depends
 * on, and turns anything unexpected into a normal application error.
 */
export class InvalidResponseError extends ApiError {
  constructor(field: string) {
    super(`Unexpected API response: ${field}`, 0, 'server')
    this.name = 'InvalidResponseError'
  }
}

export function asRecord(value: unknown, context: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new InvalidResponseError(context)
  }
  return value as Record<string, unknown>
}

export function asString(value: unknown, field: string): string {
  if (typeof value !== 'string') throw new InvalidResponseError(field)
  return value
}

export function asNumber(value: unknown, field: string): number {
  if (typeof value !== 'number' || Number.isNaN(value)) throw new InvalidResponseError(field)
  return value
}

export function asBoolean(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') throw new InvalidResponseError(field)
  return value
}

/** Optional field: the API sends `null`, the domain uses `undefined`. */
export function optional<T>(
  value: unknown,
  field: string,
  parse: (value: unknown, field: string) => T,
): T | undefined {
  if (value === null || value === undefined) return undefined
  return parse(value, field)
}

/** Closed enumeration: an unknown value means the contract changed. */
export function asEnum<T extends string>(
  value: unknown,
  field: string,
  allowed: readonly T[],
): T {
  const text = asString(value, field)
  if (!(allowed as readonly string[]).includes(text)) throw new InvalidResponseError(field)
  return text as T
}

/** Unwraps the `{ items: [...] }` envelope: a transport detail stops here. */
export function asItems(value: unknown, context: string): unknown[] {
  const payload = asRecord(value, context)
  if (!Array.isArray(payload.items)) throw new InvalidResponseError(`${context}.items`)
  return payload.items
}
