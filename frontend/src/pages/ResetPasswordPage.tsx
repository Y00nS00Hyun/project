import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiClientError } from '../api/client'
import { postJson } from '../api/client'

/**
 * Set a new password using an administrator-issued token.
 *
 * Unauthenticated, because somebody who cannot log in is exactly who needs it.
 * The token stands in for the forgotten password.
 *
 * The administrator who issued the token never learns what is chosen here --
 * that is the whole reason a token is issued instead of a temporary password
 * being set for somebody.
 */
export function ResetPasswordPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const [token, setToken] = useState(params.get('token') ?? '')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (submitting) return
    if (password !== confirm) {
      // Checked here as well so the obvious mistake does not cost a round
      // trip. The server check is the one that counts.
      setError('비밀번호가 일치하지 않습니다.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await postJson<void>('/auth/password/reset', {
        token: token.trim(), new_password: password, new_password_confirm: confirm,
      })
      setDone(true)
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught.message
          : '비밀번호를 변경하지 못했습니다. 잠시 후 다시 시도해 주세요.',
      )
    } finally {
      setSubmitting(false)
    }
  }

  if (done) {
    return (
      <main className="page auth-page">
        <h1 className="page-title">비밀번호 변경 완료</h1>
        <p className="notice">새 비밀번호로 로그인해 주세요.</p>
        <button className="button button-primary" type="button" onClick={() => navigate('/login')}>
          로그인 화면으로
        </button>
      </main>
    )
  }

  return (
    <main className="page auth-page">
      <h1 className="page-title">비밀번호 재설정</h1>
      <p className="state-hint">
        관리자에게 받은 재설정 토큰으로 새 비밀번호를 설정합니다. 완료하면 기존 로그인 세션은
        모두 종료됩니다.
      </p>

      <form className="auth-form" onSubmit={onSubmit}>
        <label className="auth-field">
          <span>재설정 토큰</span>
          <input
            type="text" value={token} onChange={(event) => setToken(event.target.value)}
            autoComplete="off" required
          />
        </label>
        <label className="auth-field">
          <span>새 비밀번호</span>
          <input
            type="password" value={password} onChange={(event) => setPassword(event.target.value)}
            autoComplete="new-password" minLength={8} maxLength={256} required
          />
        </label>
        <label className="auth-field">
          <span>새 비밀번호 확인</span>
          <input
            type="password" value={confirm} onChange={(event) => setConfirm(event.target.value)}
            autoComplete="new-password" minLength={8} maxLength={256} required
          />
        </label>

        {error && (
          <div className="state state-error" role="alert">
            <p className="state-message">{error}</p>
          </div>
        )}

        <button className="button button-primary" type="submit" disabled={submitting}>
          {submitting ? '변경 중...' : '비밀번호 설정'}
        </button>
      </form>

      <p className="auth-alt">
        <Link to="/login">로그인 화면으로</Link>
      </p>
    </main>
  )
}
