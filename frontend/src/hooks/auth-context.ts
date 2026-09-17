import { createContext, useContext } from 'react'
import type { SessionState } from '../types/auth'

export interface AuthContextValue {
  session: SessionState
  signIn: () => Promise<void>
  signOut: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)

/** Access the session. Throws if used outside the provider — a real bug. */
export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside <AuthProvider>')
  return value
}
