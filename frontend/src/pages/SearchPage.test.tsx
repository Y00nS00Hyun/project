import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SearchPage } from './SearchPage'
import {
  emptyMetadata,
  errorResponse,
  jsonResponse,
  makeItem,
  makeSearchResponse,
  mockFetch,
  renderAt,
} from '../test/helpers'

function searchUrls(): string[] {
  return vi
    .mocked(fetch)
    .mock.calls.map((call) => String(call[0]))
    .filter((url) => url.includes('/api/v1/search'))
}

describe('SearchPage', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it('browses on first visit instead of showing a blank page', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...emptyMetadata,
        '/api/v1/search': makeSearchResponse([makeItem({ snippet: null, matched_chunk: null })], {
          total: 1,
        }),
      }),
    )
    renderAt(<SearchPage />, '/search')

    expect(await screen.findByText('문서 1건')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '2026년 AI 문서관리 사업계획서' })).toBeInTheDocument()
    // Browse mode: no q parameter is sent at all.
    expect(searchUrls()[0]).not.toContain('q=')
  })

  it('shows a loading state while the first request is in flight', async () => {
    let release: (value: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...emptyMetadata,
        '/api/v1/search': () => new Promise<Response>((resolve) => (release = resolve)),
      }),
    )
    renderAt(<SearchPage />, '/search')
    expect(await screen.findByRole('status')).toHaveTextContent('문서를 불러오는 중...')
    release(jsonResponse(makeSearchResponse([])))
    await waitFor(() => expect(screen.queryByText('문서를 불러오는 중...')).not.toBeInTheDocument())
  })

  it('searches on submit, not on every keystroke', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([makeItem()]) }),
    )
    renderAt(<SearchPage />, '/search')
    await screen.findByText(/검색 결과|문서 \d+건/)

    const before = searchUrls().length
    await userEvent.type(screen.getByRole('searchbox'), '사업계획')
    // Typing eight characters must not produce eight semantic searches.
    expect(searchUrls().length).toBe(before)

    await userEvent.click(screen.getByRole('button', { name: '검색' }))
    await waitFor(() => expect(searchUrls().length).toBeGreaterThan(before))
    expect(searchUrls().at(-1)).toContain(`q=${encodeURIComponent('사업계획')}`)
  })

  it('submits on Enter', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([makeItem()]) }),
    )
    renderAt(<SearchPage />, '/search')
    await screen.findByText(/검색 결과|문서 \d+건/)

    await userEvent.type(screen.getByRole('searchbox'), '예산{Enter}')
    await waitFor(() => expect(searchUrls().at(-1)).toContain(`q=${encodeURIComponent('예산')}`))
  })

  it('renders a result with its snippet and matched position', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([makeItem()], { total: 1 }) }),
    )
    renderAt(<SearchPage />, '/search?q=예산')

    expect(await screen.findByText('검색 결과 1건')).toBeInTheDocument()
    expect(screen.getByText(/총 사업비는 300,000,000원이며/)).toBeInTheDocument()
    expect(screen.getByText('문단 42')).toBeInTheDocument()
  })

  it('sends every filter the backend supports', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([makeItem()]) }),
    )
    renderAt(<SearchPage />, '/search?year=2026&tag_id=12&file_type=hwpx')
    await screen.findByText(/문서 \d+건/)

    const url = searchUrls()[0]
    // department_id is intentionally absent: the filter was removed from the
    // UI. The backend still accepts it.
    expect(url).not.toContain('department_id')
    expect(url).toContain('year=2026')
    expect(url).toContain('tag_id=12')
    expect(url).toContain('file_type=hwpx')
  })

  it('populates filter options from the server, never from a hard-coded list', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([]) }),
    )
    renderAt(<SearchPage />, '/search')
    await screen.findByText('표시할 문서가 없습니다.')

        // Document kinds come from the server too, with the namespace stripped.
    expect(await screen.findByRole('option', { name: '매뉴얼' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '보고서' })).toBeInTheDocument()
    // A free-form tag is not a document kind and must not appear there.
    expect(screen.queryByRole('option', { name: '보안' })).not.toBeInTheDocument()
  })

  it('re-searches when a filter changes and returns to page 1', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([makeItem()], { total: 100, page: 3 }) }),
    )
    renderAt(<SearchPage />, '/search?q=예산&page=3')
    await screen.findByText('검색 결과 100건')

    await userEvent.selectOptions(await screen.findByLabelText('문서 종류'), '1')
    await waitFor(() => {
      const url = searchUrls().at(-1) ?? ''
      expect(url).toContain('tag_id=1')
      expect(url).toContain('page=1')
    })
  })

  it('offers only the file types the backend accepts', async () => {
    vi.stubGlobal('fetch', mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([]) }))
    renderAt(<SearchPage />, '/search')
    await screen.findByText('표시할 문서가 없습니다.')

    for (const type of ['HWP', 'HWPX', 'DOCX', 'PDF']) {
      expect(screen.getByRole('option', { name: type })).toBeInTheDocument()
    }
    expect(screen.queryByRole('option', { name: 'XLSX' })).not.toBeInTheDocument()
  })

  it('says only that there are no results, without guessing why', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([], { total: 0 }) }),
    )
    renderAt(<SearchPage />, '/search?q=없는문서')

    expect(await screen.findByText('검색 결과가 없습니다.')).toBeInTheDocument()
    // ACL filtering happens before retrieval; the client cannot tell the two
    // cases apart and must not imply it can.
    expect(screen.queryByText(/권한/)).not.toBeInTheDocument()
  })

  it('shows the server message and request id for an error envelope', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...emptyMetadata,
        '/api/v1/search': () =>
          errorResponse('SEARCH_QUERY_TOO_LONG', '검색어는 최대 512자까지 입력할 수 있습니다.', 422),
      }),
    )
    renderAt(<SearchPage />, '/search?q=long')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '검색어는 최대 512자까지 입력할 수 있습니다.',
    )
    expect(screen.getByText(/req-test-1/)).toBeInTheDocument()
  })

  it('shows a login prompt on 401 and no user-id field', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...emptyMetadata,
        '/api/v1/search': () => errorResponse('UNAUTHENTICATED', '인증이 필요합니다.', 401),
      }),
    )
    renderAt(<SearchPage />, '/search')

    expect(await screen.findByText('로그인이 필요합니다.')).toBeInTheDocument()
    expect(screen.queryByLabelText(/user.?id/i)).not.toBeInTheDocument()
    expect(screen.queryByPlaceholderText(/uuid/i)).not.toBeInTheDocument()
  })

  it('paginates on document-level totals', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...emptyMetadata,
        '/api/v1/search': makeSearchResponse([makeItem()], { total: 37, page: 1, size: 20 }),
      }),
    )
    renderAt(<SearchPage />, '/search?q=예산')
    await screen.findByText('검색 결과 37건')

    await userEvent.click(screen.getByRole('button', { name: '2' }))
    await waitFor(() => expect(searchUrls().at(-1)).toContain('page=2'))
  })

  it('never displays a relevance number', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({ ...emptyMetadata, '/api/v1/search': makeSearchResponse([makeItem()], { total: 1 }) }),
    )
    const { container } = renderAt(<SearchPage />, '/search?q=예산')
    await screen.findByText('검색 결과 1건')

    expect(container.textContent).not.toMatch(/정확도|유사도|confidence|score/i)
    expect(container.textContent).not.toMatch(/\d+(\.\d+)?%/)
  })

  it('discards a stale response so an older query cannot overwrite a newer one', async () => {
    const pending: ((value: Response) => void)[] = []
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...emptyMetadata,
        '/api/v1/search': () => new Promise<Response>((resolve) => pending.push(resolve)),
      }),
    )
    renderAt(<SearchPage />, '/search?q=first')
    await waitFor(() => expect(pending.length).toBe(1))

    await userEvent.type(screen.getByRole('searchbox'), 'second{Enter}')
    await waitFor(() => expect(pending.length).toBe(2))

    // Resolve the newer request first, then let the stale one land.
    pending[1](jsonResponse(makeSearchResponse([makeItem({ title: '새 결과' })], { total: 1 })))
    expect(await screen.findByText('새 결과')).toBeInTheDocument()

    pending[0](jsonResponse(makeSearchResponse([makeItem({ title: '오래된 결과' })], { total: 1 })))
    await waitFor(() => expect(screen.queryByText('오래된 결과')).not.toBeInTheDocument())
    expect(screen.getByText('새 결과')).toBeInTheDocument()
  })

  it('hides the previous results and aborts their request when filters change', async () => {
    let resolveNext: (value: Response) => void = () => {}
    const fetchMock = mockFetch({ ...emptyMetadata, '/api/v1/search': () => {
      if (searchUrls().length === 1) return jsonResponse(makeSearchResponse([makeItem()]))
      return new Promise<Response>((resolve) => { resolveNext = resolve })
    } })
    vi.stubGlobal('fetch', fetchMock)
    renderAt(<SearchPage />)
    await screen.findByText('문서 1건')
    const first = fetchMock.mock.calls.find((call) => String(call[0]).includes('/search'))
    await userEvent.selectOptions(screen.getByLabelText('파일 형식'), 'pdf')
    expect(screen.queryByText('2026년 AI 문서관리 사업계획서')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('문서를 불러오는 중...')
    expect(first?.[1]?.signal?.aborted).toBe(true)
    resolveNext(jsonResponse(makeSearchResponse([])))
    await screen.findByText('표시할 문서가 없습니다.')
  })

  it('reports filter metadata failures while preserving search results', async () => {
    vi.stubGlobal('fetch', mockFetch({
      ...emptyMetadata,
      '/api/v1/tags': () => errorResponse('INTERNAL_ERROR', '태그 목록을 불러오지 못했습니다.', 500),
      '/api/v1/search': makeSearchResponse([makeItem()]),
    }))
    renderAt(<SearchPage />)
    expect(await screen.findByRole('alert')).toHaveTextContent('태그 목록을 불러오지 못했습니다.')
    expect(await screen.findByText('문서 1건')).toBeInTheDocument()
  })

  it('retries the same query on submit after a server failure', async () => {
    let attempts = 0
    vi.stubGlobal('fetch', mockFetch({
      ...emptyMetadata,
      '/api/v1/search': () => ++attempts === 1
        ? errorResponse('INTERNAL_ERROR', '잠시 후 다시 시도해 주세요.', 500)
        : jsonResponse(makeSearchResponse([makeItem()])),
    }))
    renderAt(<SearchPage />, '/search?q=예산')
    await screen.findByRole('alert')
    await userEvent.click(screen.getByRole('button', { name: '검색' }))
    expect(await screen.findByText('검색 결과 1건')).toBeInTheDocument()
    expect(attempts).toBe(2)
  })
})
