import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

/**
 * Top-level navigation, plus who is signed in.
 *
 * The admin link appears only for an administrator. That is presentation: the
 * route guard and, more importantly, every admin endpoint check the flag for
 * themselves, so hiding the link conceals nothing that matters.
 */
export function AppNav() {
  const { user, signOut } = useAuth()

  return (
    <nav className="app-nav" aria-label="주요 메뉴">
      <NavLink to="/search" className={linkClass}>
        문서 검색
      </NavLink>
      <NavLink to="/chat" className={linkClass}>
        AI 문서 질문
      </NavLink>
      {user?.is_system_admin && (
        <NavLink to="/admin" className={linkClass}>
          사용자 관리
        </NavLink>
      )}

      {user && (
        <div className="app-nav-user">
          <span className="app-nav-name">{user.name ?? '사용자'}</span>
          <button type="button" className="button button-link" onClick={() => void signOut()}>
            로그아웃
          </button>
        </div>
      )}
    </nav>
  )
}

function linkClass({ isActive }: { isActive: boolean }): string {
  return isActive ? 'app-nav-link is-active' : 'app-nav-link'
}
