import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { App } from '../App'
import { AuthProvider } from '../auth/AuthContext'
import { LoginPage } from './LoginPage'
import { SignupPage } from './SignupPage'
import {
  errorResponse, jsonResponse, mockFetch, SIGNED_IN, SIGNED_IN_ADMIN,
} from '../test/helpers'

const ANONYMOUS = {
  '/api/v1/auth/me': () => errorResponse('UNAUTHENTICATED', '인증이 필요합니다.', 401),
  '/api/v1/auth/capability': { local_auth_enabled: true, signup_enabled: true },
}

// Never a realistic secret, and never a value that appears in any config file.
const PASSWORD = 'test-password-0001'

function renderApp(fetchMock: ReturnType<typeof vi.fn>, path = '/login') {
  vi.stubGlobal('fetch', fetchMock)
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )
}

function renderPage(fetchMock: ReturnType<typeof vi.fn>, ui = <LoginPage />) {
  vi.stubGlobal('fetch', fetchMock)
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={ui} />
          <Route path="/search" element={<p>검색 화면</p>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('LoginPage', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it('shows exactly the message the server sent on failure', async () => {
    const fetchMock = mockFetch({
      ...ANONYMOUS,
      '/api/v1/auth/login': () =>
        errorResponse('UNAUTHENTICATED', '아이디 또는 비밀번호가 올바르지 않습니다.', 401),
    })
    renderPage(fetchMock)

    await userEvent.type(await screen.findByLabelText('아이디'), 'tester')
    await userEvent.type(screen.getByLabelText('비밀번호'), PASSWORD)
    await userEvent.click(screen.getByRole('button', { name: '로그인' }))

    // The page must not compose its own wording: the server deliberately
    // returns one message for a wrong password and an unknown account, and a
    // client that reworded either could tell them apart.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '아이디 또는 비밀번호가 올바르지 않습니다.',
    )
  })

  it('relays the approval message without inventing one', async () => {
    const fetchMock = mockFetch({
      ...ANONYMOUS,
      '/api/v1/auth/login': () =>
        errorResponse('FORBIDDEN', '관리자 승인 대기 중입니다.', 403),
    })
    renderPage(fetchMock)

    await userEvent.type(await screen.findByLabelText('아이디'), 'tester')
    await userEvent.type(screen.getByLabelText('비밀번호'), PASSWORD)
    await userEvent.click(screen.getByRole('button', { name: '로그인' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('관리자 승인 대기 중입니다.')
  })

  it('sends no credentials anywhere but the login endpoint', async () => {
    const fetchMock = mockFetch({
      ...ANONYMOUS,
      '/api/v1/auth/login': () => jsonResponse(SIGNED_IN['/api/v1/auth/me']),
    })
    renderPage(fetchMock)
    await userEvent.type(await screen.findByLabelText('아이디'), 'tester')
    await userEvent.type(screen.getByLabelText('비밀번호'), PASSWORD)
    await userEvent.click(screen.getByRole('button', { name: '로그인' }))

    const leaked = fetchMock.mock.calls.filter(
      (call) =>
        !String(call[0]).includes('/auth/login') &&
        String((call[1] as RequestInit)?.body ?? '').includes(PASSWORD),
    )
    expect(leaked).toHaveLength(0)
  })

  it('keeps nothing in browser storage', async () => {
    const fetchMock = mockFetch({
      ...ANONYMOUS,
      '/api/v1/auth/login': () => jsonResponse(SIGNED_IN['/api/v1/auth/me']),
    })
    renderPage(fetchMock)
    await userEvent.type(await screen.findByLabelText('아이디'), 'tester')
    await userEvent.type(screen.getByLabelText('비밀번호'), PASSWORD)
    await userEvent.click(screen.getByRole('button', { name: '로그인' }))

    // The session is an HttpOnly cookie. There is nothing for this code to
    // store, and nothing an injected script could read back out.
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  })

  it('hides the signup link when signup is switched off', async () => {
    renderPage(mockFetch({
      ...ANONYMOUS,
      '/api/v1/auth/capability': { local_auth_enabled: true, signup_enabled: false },
    }))
    await screen.findByLabelText('아이디')
    expect(screen.queryByRole('link', { name: '회원가입' })).not.toBeInTheDocument()
  })

  it('says so when the deployment has no local login at all', async () => {
    renderPage(mockFetch({
      ...ANONYMOUS,
      '/api/v1/auth/capability': { local_auth_enabled: false, signup_enabled: false },
    }))
    expect(await screen.findByText(/로그인 기능이 비활성화/)).toBeInTheDocument()
  })
})

describe('SignupPage', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it('offers no department, role or permission control', async () => {
    renderPage(mockFetch(ANONYMOUS), <SignupPage />)
    await screen.findByLabelText('아이디')
    const labels = screen.getAllByText(/아이디|이름|비밀번호/).map((node) => node.textContent)
    expect(labels).toEqual(['아이디', '이름', '비밀번호', '비밀번호 확인'])
    expect(screen.queryByLabelText(/부서/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/권한|관리자/)).not.toBeInTheDocument()
  })

  it('reports that the account is waiting rather than logging anyone in', async () => {
    const fetchMock = mockFetch({
      ...ANONYMOUS,
      '/api/v1/auth/signup': () =>
        jsonResponse({ status: 'PENDING', message: '관리자 승인 대기 중입니다.' }, 201),
    })
    renderPage(fetchMock, <SignupPage />)

    await userEvent.type(await screen.findByLabelText('아이디'), 'tester')
    await userEvent.type(screen.getByLabelText('이름'), '테스터')
    await userEvent.type(screen.getByLabelText('비밀번호'), PASSWORD)
    await userEvent.type(screen.getByLabelText('비밀번호 확인'), PASSWORD)
    await userEvent.click(screen.getByRole('button', { name: '회원가입' }))

    expect(await screen.findByText('관리자 승인 대기 중입니다.')).toBeInTheDocument()
    expect(screen.getByText(/관리자가 승인하면/)).toBeInTheDocument()
  })

  it('catches a mismatched confirmation before sending anything', async () => {
    const fetchMock = mockFetch(ANONYMOUS)
    renderPage(fetchMock, <SignupPage />)

    await userEvent.type(await screen.findByLabelText('아이디'), 'tester')
    await userEvent.type(screen.getByLabelText('이름'), '테스터')
    await userEvent.type(screen.getByLabelText('비밀번호'), PASSWORD)
    await userEvent.type(screen.getByLabelText('비밀번호 확인'), 'test-password-9999')
    await userEvent.click(screen.getByRole('button', { name: '회원가입' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('비밀번호가 일치하지 않습니다.')
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes('/auth/signup')),
    ).toHaveLength(0)
  })
})

describe('route guard', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it.each(['/search', '/documents/doc-1', '/chat'])(
    'sends an anonymous visitor from %s to the login page',
    async (path) => {
      const fetchMock = mockFetch(ANONYMOUS)
      renderApp(fetchMock, path)
      expect(await screen.findByRole('heading', { name: '로그인' })).toBeInTheDocument()

      // The page never got far enough to ask for data, which is what replaces
      // the old screenful of separate "로그인이 필요합니다" boxes.
      const dataCalls = fetchMock.mock.calls.filter((call) => {
        const url = String(call[0])
        return !url.includes('/auth/')
      })
      expect(dataCalls).toHaveLength(0)
    },
  )

  it('shows one message, not one per panel', async () => {
    renderApp(mockFetch(ANONYMOUS), '/search')
    await screen.findByRole('heading', { name: '로그인' })
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('hides the admin link from an ordinary user', async () => {
    renderApp(mockFetch({
      ...SIGNED_IN,
      '/api/v1/folders': { items: [] },
      '/api/v1/tags': { items: [], page: 1, size: 100, total: 0 },
      '/api/v1/departments': { items: [] },
      '/api/v1/search': { items: [], page: 1, size: 20, total: 0 },
    }), '/search')
    expect(await screen.findByText('윤수현')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '사용자 관리' })).not.toBeInTheDocument()
    // The organisation does not use departments, so the strip shows a name and
    // a way out and nothing else.
    expect(screen.queryByText(/부서/)).not.toBeInTheDocument()
  })

  it('refuses the admin page to a non-administrator', async () => {
    renderApp(mockFetch(SIGNED_IN), '/admin')
    expect(await screen.findByRole('alert')).toHaveTextContent('관리자 권한이 필요합니다.')
  })
})

describe('department is gone from every authentication surface', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it('the signup form asks for four things and none of them is a department', async () => {
    renderPage(mockFetch(ANONYMOUS), <SignupPage />)
    await screen.findByLabelText('아이디')
    const { container } = { container: document.body }
    expect(container.textContent).not.toMatch(/부서/)
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('the login page never mentions one', async () => {
    renderPage(mockFetch(ANONYMOUS))
    await screen.findByLabelText('아이디')
    expect(document.body.textContent).not.toMatch(/부서/)
  })

  it('the admin screen approves without asking for one', async () => {
    const fetchMock = mockFetch({
      ...SIGNED_IN_ADMIN,
      '/api/v1/admin/users': [
        {
          user_id: 'u-2', login_id: 'waiting', name: '대기자', status: 'PENDING',
          is_system_admin: false, created_at: '2026-09-01T00:00:00Z',
        },
      ],
    })
    renderApp(fetchMock, '/admin')

    const approve = await screen.findByRole('button', { name: '승인' })
    // No department picker to satisfy first, and the button is live at once.
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(approve).toBeEnabled()
    expect(document.body.textContent).not.toMatch(/부서/)

    await userEvent.click(approve)
    const posted = fetchMock.mock.calls.find(
      (call) => String(call[0]).includes('/approve'),
    )
    expect(posted).toBeDefined()
    expect(String((posted![1] as RequestInit).body ?? '{}')).not.toMatch(/department/)
  })
})
