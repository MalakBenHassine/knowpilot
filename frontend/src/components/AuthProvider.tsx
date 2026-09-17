import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { AuthContext } from '../hooks/auth-context'
import { fetchSession, logout, startLogin } from '../services/auth'
import type { SessionState } from '../types/auth'

/** Owns the session state: the only global state in the application. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionState>({ status: 'checking' })

  useEffect(() => {
    let active = true
    void fetchSession().then((user) => {
      if (!active) return
      setSession(user ? { status: 'authenticated', user } : { status: 'anonymous' })
    })
    return () => {
      active = false
    }
  }, [])

  const signIn = useCallback(async () => {
    const user = await startLogin()
    if (user) setSession({ status: 'authenticated', user })
  }, [])

  const signOut = useCallback(async () => {
    await logout()
    setSession({ status: 'anonymous' })
  }, [])

  const value = useMemo(() => ({ session, signIn, signOut }), [session, signIn, signOut])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
