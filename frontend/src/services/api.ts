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

  constructor(message: string, status: number, kind: ChatErrorKind) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.kind = kind
  }
}

function kindFromStatus(status: number): ChatErrorKind {
  if (status === 429) return 'rate_limited'
  return 'server'
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE'
  body?: BodyInit
  headers?: Record<string, string>
  signal?: AbortSignal
  timeoutMs?: number
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, headers = {}, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options
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
    throw new ApiError(`Request failed (${response.status})`, response.status, kindFromStatus(response.status))
  }

  return (await response.json()) as T
}
