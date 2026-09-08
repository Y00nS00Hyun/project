import type { ApiClientError } from '../api/client'

export function LoadingState({ label = '검색 중...' }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  )
}

/**
 * Nothing matched.
 *
 * The message says only that. Whether documents were filtered out by
 * permissions or simply do not exist is not something the client can know --
 * the backend applies ACL before retrieval and the two cases are
 * indistinguishable here by design.
 */
export function EmptyState({ message = '검색 결과가 없습니다.' }: { message?: string }) {
  return (
    <div className="state" role="status">
      {message}
    </div>
  )
}

/**
 * Show the server's message, and nothing else.
 *
 * No stack trace, no status code dump, no raw response body. The request id is
 * shown small so a user can quote it when reporting a problem.
 */
export function ErrorView({ error }: { error: ApiClientError }) {
  if (error.isUnauthenticated) return <AuthRequired />
  return (
    <div className="state state-error" role="alert">
      <p className="state-message">{error.message}</p>
      {error.requestId && <p className="request-id">문제 신고 번호: {error.requestId}</p>}
    </div>
  )
}

/**
 * 401.
 *
 * There is no "enter your user id" form here on purpose: identity is resolved
 * by the server, and a client-supplied id would make the ACL meaningless.
 * Development identity is configured in .env.local, not typed into the UI.
 */
export function AuthRequired() {
  return (
    <div className="state state-error" role="alert">
      <p className="state-message">로그인이 필요합니다.</p>
      <p className="state-hint">사내 계정으로 로그인한 뒤 다시 시도해 주세요.</p>
    </div>
  )
}
