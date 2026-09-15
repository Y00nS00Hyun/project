import { useCallback, useState } from 'react'
import { ApiClientError } from '../api/client'
import { approveUser, disableUser, fetchAdminUsers, setUserAdmin } from '../api/auth'
import { AppNav } from '../components/AppNav'
import { ErrorView, LoadingState } from '../components/StateViews'
import { useAsyncResource } from '../hooks/useAsyncResource'
import { useAuth } from '../auth/AuthContext'
import { formatDate } from '../labels'
import type { AdminUser } from '../api/types'

/**
 * The account queue.
 *
 * Approving is the only way an account becomes usable, and it is one person
 * deciding about another -- so the screen shows who asked, when, and under
 * which login id.
 *
 * Approval grants no document permission. What an approved account may read is
 * decided in document_permissions, which this screen deliberately does not
 * edit: a permissions UI is its own piece of work, and a careless one is the
 * easiest possible way to widen access by accident.
 */
export function AdminPage() {
  const { user } = useAuth()
  const [version, setVersion] = useState(0)
  const [showAll, setShowAll] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<ApiClientError | null>(null)

  const users = useAsyncResource(
    (signal) => fetchAdminUsers(undefined, { signal }, showAll),
    [version, showAll],
  )

  const summary = users.data && {
    total: users.data.length,
    pending: users.data.filter((row) => row.status === 'PENDING').length,
    active: users.data.filter((row) => row.status === 'ACTIVE').length,
    disabled: users.data.filter((row) => row.status === 'DISABLED').length,
  }

  const run = useCallback(async (userId: string, action: () => Promise<unknown>) => {
    if (busy) return
    setBusy(userId)
    setError(null)
    try {
      await action()
      // Refetch rather than patch: the row on screen is then what the database
      // holds, including anything the server decided that we did not send.
      setVersion((value) => value + 1)
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught
          : new ApiClientError('INTERNAL_ERROR', '요청을 처리하지 못했습니다.', 0, null),
      )
    } finally {
      setBusy(null)
    }
  }, [busy])

  return (
    <main className="admin-page">
      <AppNav showBrand />

      <div className="admin-page-content">
        <header className="admin-page-header">
          <h1 className="page-title">사용자 관리</h1>
          <p className="page-description">
            가입한 사용자의 승인 상태와 관리자 권한을 관리합니다.
          </p>
          <p className="state-hint">
            승인은 로그인 허용만을 의미하며, 문서 열람 권한은 별도로 부여합니다.
            비활성화한 계정은 목록에서 사라집니다. 아래를 체크하면 다시 찾아
            <strong> 활성화</strong> 할 수 있습니다.
          </p>
        </header>

        {summary && (
          <dl className="admin-summary" aria-label="현재 사용자 목록 요약">
            <div>
              <dt>현재 목록</dt>
              <dd>{summary.total}</dd>
            </div>
            <div className={summary.pending > 0 ? 'has-pending' : undefined}>
              <dt>승인 대기</dt>
              <dd>{summary.pending}</dd>
            </div>
            <div>
              <dt>사용 중</dt>
              <dd>{summary.active}</dd>
            </div>
            {showAll && (
              <div>
                <dt>비활성</dt>
                <dd>{summary.disabled}</dd>
              </div>
            )}
          </dl>
        )}

        {/* The list answers one question by default: who could sign in right
            now? Anyone already shut out -- disabled, or a seeded user who never
            had a password -- is left out, because together they outnumbered the
            accounts that actually needed a decision. PENDING stays: that queue
            is why the screen exists.

            Nothing is deleted or changed by this; a disabled account is found
            again by checking the box, which is how it gets re-enabled. */}
        <p className="admin-filter">
          <label>
            <input
              type="checkbox"
              checked={showAll}
              onChange={(event) => setShowAll(event.target.checked)}
            />{' '}
            로그인할 수 없는 계정도 보기
          </label>
        </p>

        {error && <ErrorView error={error} />}
        {users.error && <ErrorView error={users.error} />}

        {!users.data ? (
          <LoadingState label="사용자 목록을 불러오는 중..." />
        ) : users.data.length === 0 ? (
          <p className="state admin-empty" role="status">표시할 사용자가 없습니다.</p>
        ) : (
          <div className="admin-table-panel">
            <table className="admin-table">
              <thead>
                <tr>
                  <th scope="col">사용자</th>
                  <th scope="col">상태</th>
                  <th scope="col">관리자</th>
                  <th scope="col">가입일</th>
                  <th scope="col">작업</th>
                </tr>
              </thead>
              <tbody>
                {users.data.map((row) => (
                  <UserRow
                    key={row.user_id}
                    row={row}
                    busy={busy === row.user_id}
                    isSelf={row.user_id === user?.user_id}
                    onApprove={() => run(row.user_id, () => approveUser(row.user_id))}
                    onDisable={() => run(row.user_id, () => disableUser(row.user_id))}
                    onSetAdmin={(granted) =>
                      run(row.user_id, () => setUserAdmin(row.user_id, granted))}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  )
}

function UserRow({
  row, busy, isSelf, onApprove, onDisable, onSetAdmin,
}: {
  row: AdminUser
  busy: boolean
  isSelf: boolean
  onApprove: () => void
  onDisable: () => void
  onSetAdmin: (granted: boolean) => void
}) {
  return (
    <tr>
      <td className="admin-user" data-label="사용자">
        <strong>{row.name ?? '-'}</strong>
        <span>{row.login_id ?? '-'}</span>
      </td>
      <td data-label="상태">
        <span className={`admin-status status-${row.status.toLowerCase()}`}>
          {STATUS_LABELS[row.status] ?? row.status}
        </span>
      </td>
      <td data-label="관리자">
        {row.is_system_admin ? (
          <span className="admin-role">관리자</span>
        ) : (
          <span className="admin-role-empty">-</span>
        )}
      </td>
      <td className="admin-created" data-label="가입일">{formatDate(row.created_at)}</td>
      <td className="admin-actions" data-label="작업">
        <div className="admin-action-group">
          {row.status === 'PENDING' && (
            <button type="button" className="button button-primary" disabled={busy}
                    onClick={onApprove}>
              승인
            </button>
          )}
          {row.status === 'DISABLED' && (
            // A disabled account must have a way back. Without this the row has
            // no controls at all and the only remedy is a shell on the server --
            // which is a long way to go for a mis-click.
            <button type="button" className="button button-primary" disabled={busy}
                    onClick={onApprove}>
              활성화
            </button>
          )}
          {row.status === 'ACTIVE' && !isSelf && (
            // Self is excluded from both: the server refuses an administrator
            // removing their own rights, and refuses any change that would leave
            // the installation with none at all.
            <button
                    type="button"
                    className={row.is_system_admin
                      ? 'button button-admin-revoke'
                      : 'button button-admin-grant'}
                    disabled={busy}
                    onClick={() => onSetAdmin(!row.is_system_admin)}>
              {row.is_system_admin ? '관리자 해제' : '관리자 지정'}
            </button>
          )}
          {row.status !== 'DISABLED' && !isSelf && (
            // Confirmed, unlike the others. Disabling logs the person out
            // immediately and is the one action here that takes something away
            // -- and it sits next to buttons that do not, in a list where the
            // rows look alike.
            <button
              type="button"
              className="button button-danger"
              disabled={busy}
              onClick={() => {
                if (window.confirm(`${row.name ?? row.login_id ?? '이 사용자'} 계정을 비활성화할까요?\n`
                                   + '로그인이 차단되고 사용 중인 세션이 즉시 종료됩니다.')) {
                  onDisable()
                }
              }}
            >
              비활성화
            </button>
          )}
        </div>
      </td>
    </tr>
  )
}

const STATUS_LABELS: Record<string, string> = {
  PENDING: '승인 대기',
  ACTIVE: '사용 중',
  DISABLED: '비활성',
}
