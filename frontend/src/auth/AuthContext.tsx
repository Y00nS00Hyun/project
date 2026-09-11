import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react'
import { ApiClientError } from '../api/client'
import { fetchAuthCapability, fetchCurrentUser, logout as postLogout } from '../api/auth'
import type { AuthCapability, CurrentUser } from '../api/types'

/**
 * Who is logged in, asked once for the whole app.
 *
 * Having one place ask means a page never fires its own data requests before
 * the answer is known. That is what removes the screen full of separate red
 * "로그인이 필요합니다" boxes: previously the folder tree, the result list and
 * the tag list each discovered the 401 for themselves and each reported it.
 *
 * This is convenience and nothing more. Every request is still authorised by
 * the server on its own merits -- a redirect in a browser protects nobody.
 */
interface AuthState {
  /** undefined while the first check is in flight, null when nobody is logged in. */
  user: CurrentUser | null | undefined
  capability: AuthCapability | null
  loading: boolean
  refresh: () => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null | undefined>(undefined)
  const [capability, setCapability] = useState<AuthCapability | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setUser(await fetchCurrentUser())
    } catch {
      // A 401 here is the ordinary answer on a first visit, not a failure.
      // Any other error is treated the same way: the app has nothing useful
      // to show either way, and the login page reports the real problem when
      // the user actually tries to sign in.
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    // Independent of who is logged in, and the login page needs it before
    // anybody is.
    fetchAuthCapability().then(setCapability).catch(() => setCapability(null))
  }, [load])

  const signOut = useCallback(async () => {
    try {
      await postLogout()
    } finally {
      // Cleared locally whatever the server said. A logout that appeared to
      // fail but actually succeeded would leave the UI claiming a session
      // that no longer exists.
      setUser(null)
    }
  }, [])

  const value = useMemo<AuthState>(
    () => ({ user, capability, loading, refresh: load, signOut }),
    [user, capability, loading, load, signOut],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext)
  if (value === null) throw new Error('useAuth must be used inside AuthProvider')
  return value
}

export { ApiClientError }
