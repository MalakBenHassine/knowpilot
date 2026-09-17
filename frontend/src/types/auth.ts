/** Shape returned by GET /api/auth/me. Mirrors the backend contract. */
export interface User {
  id: string
  email: string
  displayName: string
}

/**
 * Session state. Modelled as a union rather than booleans: "not yet known" and
 * "not logged in" are different situations and must not render the same way.
 */
export type SessionState =
  | { status: 'checking' }
  | { status: 'anonymous' }
  | { status: 'authenticated'; user: User }
