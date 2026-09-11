import { useCallback, useState } from 'react'
import { ApiClientError } from '../api/client'
import { approveUser, disableUser, fetchAdminUsers, setUserAdmin } from '../api/auth'
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
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<ApiClientError | null>(null)

  const users = useAsyncResource((signal) => fetchAdminUsers(undefined, { signal }), [version])

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
    <main className="page">
      <h1 className="page-title">사용자 관리</h1>
      <p className="state-hint">
        승인은 로그인 허용만을 의미하며, 문서 열람 권한은 별도로 부여합니다.
      </p>

      {error && <ErrorView error={error} />}
      {users.error && <ErrorView error={users.error} />}

      {!users.data ? (
        <LoadingState label="사용자 목록을 불러오는 중..." />
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>아이디</th><th>이름</th><th>상태</th><th>가입일</th><th>작업</th>
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
      )}
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
      <td>{row.login_id ?? '-'}</td>
      <td>{row.name ?? '-'}{row.is_system_admin && <span className="chip">관리자</span>}</td>
      <td>{STATUS_LABELS[row.status] ?? row.status}</td>
      <td>{formatDate(row.created_at)}</td>
      <td className="admin-actions">
        {row.status === 'PENDING' && (
          <button type="button" className="button button-primary" disabled={busy}
                  onClick={onApprove}>
            승인
          </button>
        )}
        {row.status === 'ACTIVE' && !isSelf && (
          // Self is excluded from both: the server refuses an administrator
          // removing their own rights, and refuses any change that would leave
          // the installation with none at all.
          <button type="button" className="button" disabled={busy}
                  onClick={() => onSetAdmin(!row.is_system_admin)}>
            {row.is_system_admin ? '관리자 해제' : '관리자 지정'}
          </button>
        )}
        {row.status !== 'DISABLED' && !isSelf && (
          <button type="button" className="button" disabled={busy} onClick={onDisable}>
            비활성화
          </button>
        )}
      </td>
    </tr>
  )
}

const STATUS_LABELS: Record<string, string> = {
  PENDING: '승인 대기',
  ACTIVE: '사용 중',
  DISABLED: '비활성',
}
