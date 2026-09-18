import type { User } from '../types/auth'
import { ApiError, request, setCsrfToken } from './api'
import { asRecord, asString } from './parse'

/**
 * Authentication always talks to the real backend: the BFF owns the OIDC flow,
 * the tokens and the session. Nothing here is mocked.
 */

interface LogoutPayload {
  logout_url: string
}

/** Current session, or null when anonymous. */
export async function fetchSession(): Promise<User | null> {
  try {
    const payload = asRecord(await request<unknown>('/auth/me'), 'auth/me')
    // The CSRF token is kept in memory only, never in storage.
    setCsrfToken(asString(payload.csrf_token, 'auth/me.csrf_token'))
    return {
      id: asString(payload.id, 'auth/me.id'),
      email: asString(payload.email, 'auth/me.email'),
      displayName: asString(payload.display_name, 'auth/me.display_name'),
    }
  } catch (error) {
    // 401 is not an error here: it simply means "not signed in yet".
    if (error instanceof ApiError && error.status === 401) {
      setCsrfToken(null)
      return null
    }
    throw error
  }
}

/**
 * Starts the OIDC login. This is a full page redirect, not a fetch: the user
 * must see Keycloak's page, and the application never handles the password.
 */
export function startLogin(): void {
  window.location.assign('/api/auth/login')
}

/**
 * Same OIDC flow, but Keycloak shows its registration form first. A successful
 * sign-up comes back authenticated, so there is no second login step.
 */
export function startRegistration(): void {
  window.location.assign('/api/auth/register')
}

/**
 * Ends our session server side, then sends the browser to Keycloak so the
 * identity provider session ends too. Without the second step, clicking
 * "sign in" again would log straight back in.
 */
export async function logout(): Promise<void> {
  try {
    const payload = await request<LogoutPayload>('/auth/logout', { method: 'POST' })
    setCsrfToken(null)
    window.location.assign(payload.logout_url)
  } catch {
    // Even if the provider is unreachable, our own session is gone.
    setCsrfToken(null)
    window.location.assign('/login')
  }
}
