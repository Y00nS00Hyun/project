import { NavLink } from 'react-router-dom'
import type { ChatSessionSummary } from '../api/types'
import { formatDate } from '../labels'

/**
 * The caller's sessions.
 *
 * Everything shown comes from the list response. There is no client-side
 * filtering by user: the endpoint already returns only the caller's own
 * sessions, and filtering here would suggest otherwise.
 */
export function SessionList({
  sessions,
  loading,
  creating,
  onCreate,
}: {
  sessions: ChatSessionSummary[]
  loading: boolean
  creating: boolean
  onCreate: () => void
}) {
  return (
    <>
      <button
        className="button button-primary chat-new-button"
        type="button"
        onClick={onCreate}
        disabled={creating}
      >
        {creating ? '만드는 중...' : '+ 새 대화'}
      </button>

      <nav aria-label="대화 목록">
        {loading && sessions.length === 0 ? (
          <p className="chat-sidebar-note" role="status">
            불러오는 중...
          </p>
        ) : sessions.length === 0 ? (
          <p className="chat-sidebar-note">아직 대화가 없습니다.</p>
        ) : (
          <ul className="session-list">
            {sessions.map((session) => (
              <li key={session.session_id}>
                <NavLink
                  to={`/chat/${session.session_id}`}
                  className={({ isActive }) =>
                    isActive ? 'session-item is-active' : 'session-item'
                  }
                >
                  {/* title is nullable in the contract and is not generated
                      from the first question, so say so plainly. */}
                  <span className="session-title">{session.title ?? '제목 없는 대화'}</span>
                  <span className="session-meta">
                    <span>{formatDate(session.updated_at)}</span>
                    <span>메시지 {session.message_count}개</span>
                  </span>
                </NavLink>
              </li>
            ))}
          </ul>
        )}
      </nav>
    </>
  )
}
