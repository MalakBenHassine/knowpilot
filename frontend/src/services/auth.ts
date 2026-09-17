import type { User } from '../types/auth'
import { API_MODE, request } from './api'
import { mockBackend } from './mockBackend'

/** Current session, or null when anonymous. */
export async function fetchSession(): Promise<User | null> {
  if (API_MODE === 'mock') return mockBackend.getSession()
  try {
    return await request<User>('/auth/me')
  } catch {
    // 401 simply means "no session": it is not an error to report.
    return null
  }
}

/**
 * Starts the OIDC login. With the real backend this is a full page redirect to
 * FastAPI, which performs the Authorization Code + PKCE exchange server side.
 */
export async function startLogin(): Promise<User | null> {
  if (API_MODE === 'mock') return mockBackend.signIn()
  window.location.assign('/api/auth/login')
  return null
}

export async function logout(): Promise<void> {
  if (API_MODE === 'mock') return mockBackend.signOut()
  window.location.assign('/api/auth/logout')
}
