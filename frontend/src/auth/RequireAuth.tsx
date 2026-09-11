import { Navigate, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { LoadingState } from '../components/StateViews'
import { useAuth } from './AuthContext'

/**
 * Renders its children only for a logged-in user.
 *
 * Not a security boundary -- the backend rejects every unauthenticated request
 * regardless of what the browser chooses to render. The point is that a page
 * which cannot succeed should not fire five requests and then show five copies
 * of the same error.
 *
 * The attempted location travels with the redirect so signing in returns the
 * user where they were going instead of dropping them on the search page.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading || user === undefined) {
    return (
      <main className="page">
        <LoadingState label="확인 중..." />
      </main>
    )
  }
  if (user === null) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  return <>{children}</>
}

/** For the admin page: logged in, and flagged as an administrator. */
export function RequireAdmin({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <AdminOnly>{children}</AdminOnly>
    </RequireAuth>
  )
}

function AdminOnly({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  if (user?.is_system_admin) return <>{children}</>
  // A hint, not a gate. Every admin endpoint checks the flag for itself, so a
  // user who reaches this route by typing the URL still gets nothing from it.
  return (
    <main className="page">
      <div className="state state-error" role="alert">
        <p className="state-message">관리자 권한이 필요합니다.</p>
      </div>
    </main>
  )
}
