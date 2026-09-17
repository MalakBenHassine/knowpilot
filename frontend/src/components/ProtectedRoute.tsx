import type { ReactNode } from 'react'
import { Navigate } from 'react-router'
import { useAuth } from '../hooks/auth-context'
import { Spinner } from './ui/Spinner'

/**
 * Convenience only: the backend re-checks authorisation on every request.
 * Hiding a route in the UI is never a security control.
 */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { session } = useAuth()

  if (session.status === 'checking') {
    return (
      <div className="flex min-h-dvh items-center justify-center text-ink-muted" role="status">
        <Spinner size={18} />
        <span className="sr-only">Checking your session…</span>
      </div>
    )
  }

  if (session.status === 'anonymous') return <Navigate to="/login" replace />

  return children
}
