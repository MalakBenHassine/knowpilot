import { LockKeyhole } from 'lucide-react'
import { useState } from 'react'
import { Navigate } from 'react-router'
import { useAuth } from '../hooks/auth-context'
import { startRegistration } from '../services/auth'
import { Button } from '../components/ui/Button'
import { Logo } from '../components/ui/Logo'
import { ThemeToggle } from '../components/ThemeToggle'

export function LoginPage() {
  const { session, signIn } = useAuth()
  const [isSigningIn, setIsSigningIn] = useState(false)

  if (session.status === 'authenticated') return <Navigate to="/documents" replace />

  async function handleSignIn() {
    setIsSigningIn(true)
    try {
      await signIn()
    } finally {
      setIsSigningIn(false)
    }
  }

  return (
    <div className="flex min-h-dvh flex-col bg-bg px-4">
      <header className="flex items-center justify-between py-5">
        <div className="flex items-center gap-2">
          <Logo size={22} />
          <span className="font-semibold tracking-tight">KnowPilot</span>
        </div>
        <ThemeToggle />
      </header>

      <main className="flex flex-1 items-center justify-center pb-16">
        <div className="w-full max-w-sm animate-rise">
          <h1 className="text-display font-semibold text-ink">
            Your private AI knowledge assistant
          </h1>
          <p className="mt-3 text-body text-ink-muted">
            Ask questions about your documents and get answers grounded in your sources.
          </p>

          <Button
            className="mt-8 w-full"
            isLoading={isSigningIn}
            onClick={() => void handleSignIn()}
          >
            Continue with Keycloak
          </Button>

          <p className="mt-4 text-center text-caption text-ink-muted">
            New here?{' '}
            <button
              type="button"
              onClick={startRegistration}
              className="font-medium text-accent underline underline-offset-2 hover:text-accent-hover"
            >
              Create an account
            </button>
          </p>

          <p className="mt-6 flex items-center justify-center gap-2 text-caption text-ink-subtle">
            <LockKeyhole size={13} />
            Your documents remain private.
          </p>
        </div>
      </main>
    </div>
  )
}
