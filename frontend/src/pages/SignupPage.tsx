import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiClientError } from '../api/client'
import { signup } from '../api/auth'
import { useAuth } from '../auth/AuthContext'

/**
 * Create an account.
 *
 * Four fields, and deliberately no more. There is no role and no permission
 * control here, because neither is the applicant's to choose. The API would
 * reject such a field anyway; the form simply has nothing to send.
 *
 * Signing up does not sign anyone in. The account is created pending, so the
 * page reports that and stops rather than pretending to have granted access.
 */
export function SignupPage() {
  const { capability } = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState({
    login_id: '', name: '', password: '', password_confirm: '',
  })
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  function update(field: keyof typeof form) {
    return (event: { target: { value: string } }) =>
      setForm((current) => ({ ...current, [field]: event.target.value }))
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (submitting) return
    if (form.password !== form.password_confirm) {
      // Checked here as well as on the server so the obvious mistake does not
      // cost a round trip. The server check is the one that counts.
      setError('비밀번호가 일치하지 않습니다.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const result = await signup({
        login_id: form.login_id.trim(),
        name: form.name.trim(),
        password: form.password,
        password_confirm: form.password_confirm,
      })
      setDone(result.message)
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught.message
          : '회원가입에 실패했습니다. 잠시 후 다시 시도해 주세요.',
      )
    } finally {
      setSubmitting(false)
    }
  }

  if (done) {
    return (
      <main className="page auth-page">
        <h1 className="page-title">회원가입 완료</h1>
        <p className="notice">{done}</p>
        <p className="state-hint">
          관리자가 승인하면 로그인할 수 있습니다.
        </p>
        <button className="button button-primary" type="button" onClick={() => navigate('/login')}>
          로그인 화면으로
        </button>
      </main>
    )
  }

  return (
    <main className="page auth-page">
      <h1 className="page-title">회원가입</h1>

      {capability !== null && !capability.signup_enabled && (
        <p className="notice">이 환경에서는 회원가입이 비활성화되어 있습니다.</p>
      )}
      <p className="state-hint">
        가입 후 관리자 승인을 거쳐야 로그인할 수 있습니다.
      </p>

      <form className="auth-form" onSubmit={onSubmit}>
        <label className="auth-field">
          <span>아이디</span>
          <input
            type="text" value={form.login_id} onChange={update('login_id')}
            autoComplete="username" minLength={3} maxLength={100} required
          />
        </label>
        <label className="auth-field">
          <span>이름</span>
          <input
            type="text" value={form.name} onChange={update('name')}
            autoComplete="name" maxLength={100} required
          />
        </label>
        <label className="auth-field">
          <span>비밀번호</span>
          <input
            type="password" value={form.password} onChange={update('password')}
            autoComplete="new-password" minLength={8} maxLength={256} required
          />
        </label>
        <label className="auth-field">
          <span>비밀번호 확인</span>
          <input
            type="password" value={form.password_confirm} onChange={update('password_confirm')}
            autoComplete="new-password" minLength={8} maxLength={256} required
          />
        </label>

        {error && (
          <div className="state state-error" role="alert">
            <p className="state-message">{error}</p>
          </div>
        )}

        <button className="button button-primary" type="submit" disabled={submitting}>
          {submitting ? '가입 중...' : '회원가입'}
        </button>
      </form>

      <p className="auth-alt">
        이미 계정이 있으신가요? <Link to="/login">로그인</Link>
      </p>
    </main>
  )
}
