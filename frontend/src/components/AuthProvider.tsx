import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { AuthContext } from '../hooks/auth-context'
import { fetchSession, logout, startLogin } from '../services/auth'
import type { SessionState } from '../types/auth'

/** Owns the session state: the only global state in the application. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionState>({ status: 'checking' })

  useEffect(() => {
    let active = true
    void fetchSession()
      .then((user) => {
        if (!active) return
        setSession(user ? { status: 'authenticated', user } : { status: 'anonymous' })
      })
      .catch(() => {
        // The API is unreachable: treat it as anonymous rather than hanging on
        // a spinner forever.
        if (active) setSession({ status: 'anonymous' })
      })
    return () => {
      active = false
    }
  }, [])

  // Both actions leave the page: the browser follows a redirect, so there is
  // no state to update afterwards.
  const signIn = useCallback(async () => startLogin(), [])
  const signOut = useCallback(async () => logout(), [])

  const value = useMemo(() => ({ session, signIn, signOut }), [session, signIn, signOut])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
