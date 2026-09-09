import { NavLink } from 'react-router-dom'

/**
 * Top-level navigation between the two screens.
 *
 * Deliberately two links and nothing else: search and chat are separate tools
 * over the same corpus, not a hierarchy, and the search page's own layout is
 * left untouched apart from this strip.
 */
export function AppNav() {
  return (
    <nav className="app-nav" aria-label="주요 메뉴">
      <NavLink
        to="/search"
        className={({ isActive }) => (isActive ? 'app-nav-link is-active' : 'app-nav-link')}
      >
        문서 검색
      </NavLink>
      <NavLink
        to="/chat"
        className={({ isActive }) => (isActive ? 'app-nav-link is-active' : 'app-nav-link')}
      >
        AI 문서 질문
      </NavLink>
    </nav>
  )
}
