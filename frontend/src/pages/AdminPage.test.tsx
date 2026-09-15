import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AdminPage } from './AdminPage'
import { mockFetch, renderAt, SIGNED_IN_ADMIN } from '../test/helpers'

const PATH = '/admin'

function stub(users: unknown[] = []) {
  // GET /admin/users returns a bare array, not a pagination envelope.
  vi.stubGlobal('fetch', mockFetch({
    ...SIGNED_IN_ADMIN,
    '/api/v1/admin/users': users,
  }))
}

function adminCalls() {
  return vi.mocked(fetch).mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes('/admin/users'))
}

describe('AdminPage', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it('asks only for accounts that can sign in', async () => {
    // The default list answers one question: who could sign in right now?
    // Anyone already shut out -- disabled, or a seeded user who never had a
    // password -- is left out. The server decides that; the page just asks.
    stub()
    renderAt(<AdminPage />, PATH, PATH)

    await waitFor(() => expect(adminCalls()).toHaveLength(1))
    expect(adminCalls()[0]).toBe('/api/v1/admin/users')
  })

  it('asks for everything once the reader turns the filter off', async () => {
    stub()
    renderAt(<AdminPage />, PATH, PATH)
    await waitFor(() => expect(adminCalls()).toHaveLength(1))

    await userEvent.click(
      await screen.findByRole('checkbox', { name: /로그인할 수 없는 계정도 보기/ }),
    )

    await waitFor(() => expect(adminCalls()).toHaveLength(2))
    expect(adminCalls()[1]).toBe('/api/v1/admin/users?include_loginless=true')
  })

  it('tells the reader where a disabled account went', async () => {
    // Disabling makes the row vanish from this list, so the screen has to say
    // that and where to find it again -- otherwise an accidental click looks
    // like the account was destroyed.
    stub()
    renderAt(<AdminPage />, PATH, PATH)
    await screen.findByRole('heading', { name: '사용자 관리' })

    expect(screen.getByText(/비활성화한 계정은 목록에서 사라집니다/)).toBeInTheDocument()
  })

  it('starts with the filter on', async () => {
    stub()
    renderAt(<AdminPage />, PATH, PATH)
    expect(
      await screen.findByRole('checkbox', { name: /로그인할 수 없는 계정도 보기/ }),
    ).not.toBeChecked()
  })

})
