/**
 * The only module that knows about HTTP. Components and hooks never call
 * fetch directly, so timeouts, credentials and error mapping live in one place.
 */
import type { ChatErrorKind } from '../types/chat'

export const API_MODE = import.meta.env.VITE_API_MODE ?? 'mock'

/** Same-origin base path: the reverse proxy (and the Vite dev proxy) route it. */
const BASE_URL = '/api'
const DEFAULT_TIMEOUT_MS = 30_000

export class ApiError extends Error {
  readonly status: number
  readonly kind: ChatErrorKind
  /**
   * Seconds until the request is worth repeating, when the server said so.
   * A 429 or a busy 503 carries it, because the backend sets Retry-After: a
   * refusal that does not say when to come back invites an immediate retry,
   * which is refused again.
   */
  readonly retryAfterSeconds?: number

  constructor(message: string, status: number, kind: ChatErrorKind, retryAfterSeconds?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.kind = kind
    this.retryAfterSeconds = retryAfterSeconds
  }
}

function kindFromStatus(status: number, retryAfterSeconds?: number): ChatErrorKind {
  if (status === 429) return 'rate_limited'
  // A 503 that says when to come back is an overload, not a malfunction.
  if (status === 503 && retryAfterSeconds !== undefined) return 'busy'
  return 'server'
}

/**
 * Reads Retry-After, treating it as untrusted input like any other header.
 * A missing, malformed or negative value simply means "we were not told".
 */
function retryAfterFrom(response: Response): number | undefined {
  const raw = response.headers.get('Retry-After')
  if (!raw) return undefined
  const seconds = Number(raw)
  return Number.isFinite(seconds) && seconds > 0 ? seconds : undefined
}

/**
 * CSRF token for the current session. Kept in memory on purpose: storing it in
 * localStorage would hand it to any XSS, which is exactly what it defends
 * against. It is re-read from GET /auth/me on every page load.
 */
let csrfToken: string | null = null

export function setCsrfToken(token: string | null): void {
  csrfToken = token
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE'
  body?: BodyInit
  headers?: Record<string, string>
  signal?: AbortSignal
  timeoutMs?: number
}

/** A JSON request: the common case. */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options)
  return (await response.json()) as T
}

/**
 * A request whose body the caller reads itself - a Server-Sent Events stream.
 *
 * Same credentials, same CSRF token, same error mapping as `request`: a stream
 * is not a second way into the API with its own, weaker rules. The timeout
 * covers the WHOLE stream, body included, so a server that opens a stream and
 * then goes silent cannot hold the tab for ever.
 */
export async function openStream(path: string, options: RequestOptions = {}): Promise<Response> {
  return send(path, {
    ...options,
    headers: { Accept: 'text/event-stream', ...options.headers },
  })
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const { method = 'GET', body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options
  // Copied: the caller's object is never mutated with our CSRF header.
  const headers = { ...options.headers }

  // Safe methods never change state, so they need no CSRF token.
  if (method !== 'GET' && csrfToken) {
    headers['X-CSRF-Token'] = csrfToken
  }
  const timeout = AbortSignal.timeout(timeoutMs)
  const abortSignal = signal ? AbortSignal.any([signal, timeout]) : timeout

  let response: Response
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method,
      body,
      headers,
      // Sends the httpOnly session cookie issued by the BFF.
      credentials: 'include',
      signal: abortSignal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'TimeoutError') {
      throw new ApiError('The request timed out.', 0, 'timeout')
    }
    throw new ApiError('Network request failed.', 0, 'network')
  }

  if (!response.ok) {
    // Backend details are never surfaced to the user, only the status class.
    const retryAfter = retryAfterFrom(response)
    throw new ApiError(
      `Request failed (${response.status})`,
      response.status,
      kindFromStatus(response.status, retryAfter),
      retryAfter,
    )
  }

  return response
}
