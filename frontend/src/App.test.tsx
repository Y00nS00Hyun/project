import { afterEach, expect, it, vi } from 'vitest'
import { screen, render, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { App } from './App'
import { emptyMetadata, makeDetail,
  NO_CHAT_SESSIONS,
  SIGNED_IN, makeItem, makeRevision, makeSearchResponse, mockFetch } from './test/helpers'

afterEach(() => vi.unstubAllGlobals())

function HistoryControls() {
  const location = useLocation()
  const navigate = useNavigate()
  return <>
    <output aria-label="현재 URL">{location.pathname}{location.search}</output>
    <button type="button" onClick={() => navigate(-1)}>브라우저 뒤로</button>
  </>
}

it('preserves search conditions through detail navigation and browser history', async () => {
  vi.stubGlobal('fetch', mockFetch({
    // App applies the route guard, so these pages only render for a signed-in
    // user -- which is the point of the guard and has to be set up for.
    ...SIGNED_IN,
    ...NO_CHAT_SESSIONS,
    ...emptyMetadata,
    '/api/v1/search': makeSearchResponse([makeItem()], { page: 2, total: 30 }),
    '/api/v1/documents/doc-1/revisions': { items: [makeRevision()], page: 1, size: 20, total: 1 },
    '/api/v1/documents/doc-1': makeDetail(),
  }))
  const url = '/search?q=예산&year=2026&tag_id=12&page=2'
  render(<MemoryRouter initialEntries={[url]}><HistoryControls /><App /></MemoryRouter>)
  await userEvent.click(await screen.findByRole('link', { name: '문서 보기' }))
  await screen.findByRole('heading', { name: makeDetail().title })
  expect(screen.getByRole('link', { name: '← 검색으로' })).toHaveAttribute('href', url)
  await userEvent.click(screen.getByRole('link', { name: '← 검색으로' }))
  expect(await screen.findByRole('searchbox')).toHaveValue('예산')
  expect(screen.getByLabelText('연도')).toHaveValue('2026')
  await userEvent.type(screen.getByRole('searchbox'), '수정{Enter}')
  await waitFor(() => expect(screen.getByLabelText('현재 URL')).not.toHaveTextContent('page=2'))
  await userEvent.click(screen.getByRole('button', { name: '브라우저 뒤로' }))
  await waitFor(() => expect(screen.getByRole('searchbox')).toHaveValue('예산'))
  expect(screen.getByLabelText('현재 URL')).toHaveTextContent('page=2')
})
