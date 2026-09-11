import { useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { ApiClientError } from '../api/client'
import { login } from '../api/auth'
import { useAuth } from '../auth/AuthContext'

/**
 * Sign in.
 *
 * Every failure is shown exactly as the server worded it. The server returns
 * one message for a wrong password and for an account that does not exist, so
 * this page cannot accidentally tell the two apart -- there is nothing here
 * that inspects the reason and picks its own wording.
 */
export function LoginPage() {
  const { user, capability, refresh } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [loginId, setLoginId] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const state: unknown = location.state
  const from =
    state && typeof state === 'object' && 'from' in state && typeof state.from === 'string'
      ? state.from
      : '/search'

  if (user) return <Navigate to={from} replace />

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError(null)
    try {
      await login({ login_id: loginId.trim(), password })
      // Re-read /auth/me rather than trusting the login response: from here on
      // the app runs on whatever the session actually resolves to.
      await refresh()
      navigate(from, { replace: true })
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught.message
          : '로그인에 실패했습니다. 잠시 후 다시 시도해 주세요.',
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="page auth-page">
      <h1 className="page-title">로그인</h1>

      {capability !== null && !capability.local_auth_enabled && (
        <p className="notice">이 환경에서는 로그인 기능이 비활성화되어 있습니다.</p>
      )}

      <form className="auth-form" onSubmit={onSubmit}>
        <label className="auth-field">
          <span>아이디</span>
          <input
            type="text"
            value={loginId}
            onChange={(event) => setLoginId(event.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label className="auth-field">
          <span>비밀번호</span>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {error && (
          <div className="state state-error" role="alert">
            <p className="state-message">{error}</p>
          </div>
        )}

        <button className="button button-primary" type="submit" disabled={submitting}>
          {submitting ? '로그인 중...' : '로그인'}
        </button>
      </form>

      {capability?.signup_enabled && (
        <p className="auth-alt">
          계정이 없으신가요? <Link to="/signup">회원가입</Link>
        </p>
      )}
    </main>
  )
}
