import { FileText, LogOut, MessageSquare, PanelLeftClose, PanelLeftOpen, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { useAuth } from '../hooks/auth-context'
import { useDocumentsContext } from '../hooks/documents-context'
import { cn } from '../lib/cn'
import { ThemeToggle } from '../components/ThemeToggle'
import { Logo } from '../components/ui/Logo'

const NAV_ITEMS = [
  { to: '/documents', label: 'Documents', icon: FileText },
  { to: '/chat', label: 'Chat', icon: MessageSquare },
]

export function AppShell() {
  const { session, signOut } = useAuth()
  const { readyCount } = useDocumentsContext()
  const [isCollapsed, setIsCollapsed] = useState(false)
  const [isDrawerOpen, setIsDrawerOpen] = useState(false)

  // Escape closes the drawer: expected keyboard behaviour for an overlay.
  useEffect(() => {
    if (!isDrawerOpen) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsDrawerOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isDrawerOpen])

  const user = session.status === 'authenticated' ? session.user : null

  const sidebar = (
    <div className="flex h-full flex-col gap-1 p-3">
      <div className={cn('flex items-center gap-2 px-2 py-2', isCollapsed && 'lg:justify-center lg:px-0')}>
        <Logo size={22} />
        <span className={cn('font-semibold tracking-tight text-ink', isCollapsed && 'lg:hidden')}>
          KnowPilot
        </span>
        <button
          type="button"
          onClick={() => setIsDrawerOpen(false)}
          aria-label="Close menu"
          className="ml-auto rounded-sm p-1 text-ink-subtle hover:text-ink lg:hidden"
        >
          <X size={18} />
        </button>
      </div>

      <nav className="mt-2 flex flex-col gap-1" aria-label="Main">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            // Navigating always closes the mobile drawer: handled in the event,
            // not in an effect.
            onClick={() => setIsDrawerOpen(false)}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-3 rounded-md px-3 py-2 text-body transition-colors duration-150',
                isActive
                  ? 'bg-accent-soft font-medium text-accent'
                  : 'text-ink-muted hover:bg-surface-sunken hover:text-ink',
                isCollapsed && 'lg:justify-center lg:px-0',
              )
            }
          >
            <Icon size={17} className="shrink-0" />
            <span className={cn(isCollapsed && 'lg:hidden')}>{label}</span>
            {label === 'Documents' && readyCount > 0 ? (
              <span
                className={cn(
                  'ml-auto rounded-sm bg-surface-sunken px-1.5 text-caption text-ink-muted',
                  isCollapsed && 'lg:hidden',
                )}
              >
                {readyCount}
              </span>
            ) : null}
          </NavLink>
        ))}
      </nav>

      <div className="mt-auto flex flex-col gap-3 border-t border-line pt-3">
        <div className={cn('flex items-center gap-2 px-1', isCollapsed && 'lg:justify-center')}>
          <ThemeToggle />
        </div>
        <div className={cn('flex items-center gap-2 px-1', isCollapsed && 'lg:flex-col lg:gap-1')}>
          <div className={cn('min-w-0 flex-1', isCollapsed && 'lg:hidden')}>
            <p className="truncate text-caption font-medium text-ink">{user?.displayName}</p>
            <p className="truncate text-caption text-ink-subtle">{user?.email}</p>
          </div>
          <button
            type="button"
            onClick={() => void signOut()}
            aria-label="Sign out"
            className="rounded-sm p-2 text-ink-subtle transition-colors hover:bg-surface-sunken hover:text-ink"
          >
            <LogOut size={16} />
          </button>
        </div>
      </div>
    </div>
  )

  return (
    <div className="flex min-h-dvh bg-bg">
      {/* Desktop sidebar */}
      <aside
        className={cn(
          'hidden shrink-0 border-r border-line bg-surface transition-[width] duration-200 lg:block',
          isCollapsed ? 'w-16' : 'w-60',
        )}
      >
        <div className="sticky top-0 h-dvh">{sidebar}</div>
      </aside>

      {/* Mobile drawer */}
      {isDrawerOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            aria-label="Close menu"
            onClick={() => setIsDrawerOpen(false)}
            className="absolute inset-0 bg-black/40 animate-fade-in"
          />
          <div className="absolute inset-y-0 left-0 w-72 border-r border-line bg-surface animate-rise">
            {sidebar}
          </div>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-line bg-bg/85 px-4 backdrop-blur">
          <button
            type="button"
            onClick={() => setIsDrawerOpen(true)}
            aria-label="Open menu"
            aria-expanded={isDrawerOpen}
            className="rounded-sm p-2 text-ink-muted hover:bg-surface-sunken hover:text-ink lg:hidden"
          >
            <PanelLeftOpen size={18} />
          </button>
          <button
            type="button"
            onClick={() => setIsCollapsed((value) => !value)}
            aria-label={isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            className="hidden rounded-sm p-2 text-ink-muted hover:bg-surface-sunken hover:text-ink lg:block"
          >
            {isCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
          </button>
          <div className="flex items-center gap-2 lg:hidden">
            <Logo size={20} />
            <span className="font-semibold tracking-tight">KnowPilot</span>
          </div>
        </header>

        <main className="min-w-0 flex-1">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
