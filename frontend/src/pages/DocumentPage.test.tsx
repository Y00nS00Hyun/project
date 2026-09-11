import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DocumentPage } from './DocumentPage'
import {
  errorResponse,
  jsonResponse,
  makeDetail,
  NO_CHAT_SESSIONS,
  makeRevision,
  makeTextBlock,
  makeTextPage,
  mockFetch,
  renderAt,
} from '../test/helpers'

const PATH = '/documents/doc-1'
const ROUTE = '/documents/:documentId'

function revisionsResponse(items = [makeRevision()]) {
  return { items, page: 1, size: 20, total: items.length }
}

function fileResponse(): Response {
  return new Response('bytes', {
    status: 200,
    headers: {
      'Content-Disposition': 'attachment; filename="plan.hwpx"',
      'Content-Type': 'application/hwp+zip',
    },
  })
}

describe('DocumentPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    // jsdom implements neither, and the download path uses both.
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:doc-1'),
      revokeObjectURL: vi.fn(),
    }))
  })
  afterEach(() => vi.unstubAllGlobals())

  it('shows the document metadata the API returns', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    expect(await screen.findByRole('heading', { name: '2026년 AI 문서관리 사업계획서' })).toBeInTheDocument()
    expect(screen.getByText('HWPX')).toBeInTheDocument()
    expect(screen.getByText('보안')).toBeInTheDocument()
  })

  it('does not show the document department', async () => {
    // The fixture still carries one, so this fails if the row comes back
    // rather than passing because nothing was sent.
    vi.stubGlobal('fetch', mockFetch({
      ...NO_CHAT_SESSIONS,
      '/api/v1/documents/doc-1/revisions': revisionsResponse(),
      '/api/v1/documents/doc-1': makeDetail(),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)

    await screen.findByRole('heading', { name: '2026년 AI 문서관리 사업계획서' })
    expect(screen.queryByText('기획조정실')).not.toBeInTheDocument()
    expect(screen.queryByText('부서')).not.toBeInTheDocument()
  })

  it('warns, and names both revisions, when search is serving the older one', async () => {
    // Rev 2 is what search serves; Rev 3 exists on disk but is not READY yet.
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    // Read each off its own <dt> so the revision list mentioning the same
    // text cannot satisfy the assertion.
    const served = await screen.findByText('검색에 사용 중인 버전', { selector: 'dt' })
    expect(served.nextElementSibling).toHaveTextContent('Rev 2')
    const detected = screen.getByText('최신 감지 버전', { selector: 'dt' })
    expect(detected.nextElementSibling).toHaveTextContent('Rev 3')
    expect(detected.nextElementSibling).toHaveTextContent('처리 중')

    expect(screen.getByText(/최신 파일을 처리 중입니다/))
      .toHaveTextContent('현재 검색에는 이전 버전이 사용되고 있습니다')
  })

  it('says nothing about versions when search is already serving the newest', async () => {
    // The warning exists to catch the reader whose edit is not searchable
    // yet. When there is nothing to catch, both the notice and the second
    // revision row are noise -- the number would simply be printed twice.
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail({
          latest_revision: {
            revision_id: 'rev-2', revision_no: 2, created_at: '2026-08-30T04:12:00Z',
          },
        }),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    await screen.findByRole('heading', { name: makeDetail().title })
    expect(screen.queryByText(/최신 파일을 처리 중입니다/)).not.toBeInTheDocument()
    expect(screen.queryByText('최신 감지 버전', { selector: 'dt' })).not.toBeInTheDocument()
    // ...while the row that says what search is actually using stays.
    expect(screen.getByText('검색에 사용 중인 버전', { selector: 'dt' })).toBeInTheDocument()
  })

  it('does not claim an older version is in use when there is none', async () => {
    // First revision still being processed: nothing is searchable yet, so
    // "현재 검색에는 이전 버전이 사용되고 있습니다" would be untrue.
    vi.stubGlobal('fetch', mockFetch({
      ...NO_CHAT_SESSIONS,
      '/api/v1/documents/doc-1/revisions': revisionsResponse(),
      '/api/v1/documents/doc-1': makeDetail({
        current_revision: null,
        is_searchable: false,
      }),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)

    await screen.findByRole('heading', { name: makeDetail().title })
    expect(screen.queryByText(/최신 파일을 처리 중입니다/)).not.toBeInTheDocument()
    expect(screen.queryByText('최신 감지 버전', { selector: 'dt' })).not.toBeInTheDocument()
    // The accurate notice instead.
    expect(screen.getByText(/검색에 사용할 수 있는 본문이 없습니다/)).toBeInTheDocument()
    expect(screen.getByText('검색에 사용 중인 버전', { selector: 'dt' }).nextElementSibling)
      .toHaveTextContent('검색 불가')
  })

  it('every figure in the grid describes the served revision', async () => {
    // The grid used to mix in a number about a different revision, which is
    // why it had to be read twice to see which was which.
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)
    await screen.findByRole('heading', { name: makeDetail().title })

    const labels = Array.from(document.querySelectorAll('.detail-grid dt'))
      .map((node) => node.textContent)
    expect(labels).toEqual([
      '파일 형식', '문서 작성일', '시스템 등록일', '원본 파일 수정일',
      '파일 크기', '검색에 사용 중인 버전', '최신 감지 버전',
    ])
  })

  it('shows what a download would actually fetch', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail({ file_size: 9215488 }),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)
    const size = await screen.findByText('파일 크기', { selector: 'dt' })
    expect(size.nextElementSibling).toHaveTextContent('8.8 MB')
  })

  it('says so when the size is unknown rather than showing zero', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail({ file_size: null }),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)
    const size = await screen.findByText('파일 크기', { selector: 'dt' })
    expect(size.nextElementSibling).toHaveTextContent('알 수 없음')
  })

  it('renders version history with localised, unmerged statuses', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse([
          makeRevision({
            revision_id: 'rev-3',
            revision_no: 3,
            is_current: false,
            is_ready: false,
            parse_status: 'FAILED',
            parse_result_code: 'PARSE_FAILED',
          }),
          makeRevision(),
          makeRevision({
            revision_id: 'rev-1',
            revision_no: 1,
            is_current: false,
            is_ready: false,
            parse_status: 'SUCCESS',
            parse_result_code: 'OCR_REQUIRED',
          }),
        ]),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    await userEvent.click(await screen.findByRole('button', { name: /버전 이력/ }))
    expect(await screen.findByText('Rev 3')).toBeInTheDocument()
    expect(screen.getByText('처리 실패')).toBeInTheDocument()
    expect(screen.getAllByText('현재 검색 버전').length).toBeGreaterThan(0)
    // SUCCESS + OCR_REQUIRED is not a failure.
    expect(screen.getByText('처리 완료')).toBeInTheDocument()
    expect(screen.getByText(/이미지 문서\(OCR 필요\)/)).toBeInTheDocument()
  })

  it('downloads by document id alone and uses the server-supplied filename', async () => {
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/download': fileResponse,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    await userEvent.click(await screen.findByRole('button', { name: '원본 다운로드' }))
    await waitFor(() => expect(anchorClick).toHaveBeenCalled())

    const downloadUrl = vi
      .mocked(fetch)
      .mock.calls.map((call) => String(call[0]))
      .find((url) => url.includes('/download'))
    // Only the id goes out: no path, no root, no filename guess.
    expect(downloadUrl).toBe('/api/v1/documents/doc-1/download')
    expect(URL.createObjectURL).toHaveBeenCalled()
  })

  it('shows the server error message when a download is refused', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/download': () =>
          errorResponse('DOCUMENT_NOT_DOWNLOADABLE', '원본 파일을 찾을 수 없습니다.', 409),
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    await userEvent.click(await screen.findByRole('button', { name: '원본 다운로드' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('원본 파일을 찾을 수 없습니다.')
  })

  it('disables the button when the API says the document is not downloadable', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail({ downloadable: false }),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)
    expect(await screen.findByRole('button', { name: '원본 다운로드' })).toBeDisabled()
  })

  it('treats 404 as "not found" without guessing that it was a permission problem', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': () =>
          errorResponse('DOCUMENT_NOT_FOUND', '문서를 찾을 수 없습니다.', 404),
        '/api/v1/documents/doc-1': () =>
          errorResponse('DOCUMENT_NOT_FOUND', '문서를 찾을 수 없습니다.', 404),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    expect(await screen.findByText('문서를 찾을 수 없습니다.')).toBeInTheDocument()
    // The backend returns 404 for "absent" and "not permitted" alike, so the
    // client cannot and must not distinguish them.
    expect(screen.queryByText(/권한이 없습니다/)).not.toBeInTheDocument()
  })

  it('prompts for login on 401', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': () =>
          errorResponse('UNAUTHENTICATED', '인증이 필요합니다.', 401),
        '/api/v1/documents/doc-1': () =>
          errorResponse('UNAUTHENTICATED', '인증이 필요합니다.', 401),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)
    expect(await screen.findByText('로그인이 필요합니다.')).toBeInTheDocument()
  })

  it('exposes no filesystem path or internal identifier', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        ...NO_CHAT_SESSIONS,
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    const { container } = renderAt(<DocumentPage />, PATH, ROUTE)
    await screen.findByRole('heading', { name: '2026년 AI 문서관리 사업계획서' })

    const text = container.textContent ?? ''
    expect(text).not.toMatch(/\/mnt|\/srv|[A-Z]:\\|\\\\/)
    // content_hash is in the revision payload but is internal detail.
    expect(text).not.toContain('a'.repeat(64))
  })

  it('fetches older revisions using the history pagination envelope', async () => {
    let page = 0
    vi.stubGlobal('fetch', mockFetch({
        ...NO_CHAT_SESSIONS,
      '/api/v1/documents/doc-1/revisions': () => jsonResponse({
        items: [makeRevision({ revision_no: ++page === 1 ? 21 : 1 })],
        page, size: 20, total: 21,
      }),
      '/api/v1/documents/doc-1': makeDetail(),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    await userEvent.click(await screen.findByRole('button', { name: /버전 이력/ }))
    await screen.findByText('Rev 21')
    await userEvent.click(screen.getByRole('button', { name: '다음' }))
    expect(await screen.findByText('Rev 1')).toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.some((call) =>
      String(call[0]).endsWith('/revisions?page=2&size=20'))).toBe(true)
    expect(screen.queryByText('Rev 21')).not.toBeInTheDocument()
  })

  it('uses the login state when authentication expires during download', async () => {
    vi.stubGlobal('fetch', mockFetch({
        ...NO_CHAT_SESSIONS,
      '/api/v1/documents/doc-1/download': () => errorResponse('UNAUTHENTICATED', '인증 만료', 401),
      '/api/v1/documents/doc-1/revisions': revisionsResponse(),
      '/api/v1/documents/doc-1': makeDetail(),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    await userEvent.click(await screen.findByRole('button', { name: '원본 다운로드' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('로그인이 필요합니다.')
  })
})

describe('DocumentPage version history', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  function renderWith(total: number, items = [makeRevision()]) {
    vi.stubGlobal('fetch', mockFetch({
      ...NO_CHAT_SESSIONS,
      '/api/v1/documents/doc-1/revisions': { items, page: 1, size: 20, total },
      '/api/v1/documents/doc-1': makeDetail(),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    return screen.findByRole('heading', { name: makeDetail().title })
  }

  it('hides the section entirely for a document ingested once', async () => {
    // One revision means the history restates the page above it.
    await renderWith(1)
    expect(screen.queryByText(/버전 이력/)).not.toBeInTheDocument()
  })

  it('shows a collapsed, counted section once there is a sequence', async () => {
    await renderWith(4)
    const toggle = screen.getByRole('button', { name: /버전 이력/ })
    expect(toggle).toHaveTextContent('버전 이력 (4)')
    // Collapsed to start: the rows are available, not in the way.
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Rev 2')).not.toBeInTheDocument()

    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByText('Rev 2')).toBeInTheDocument()

    await userEvent.click(toggle)
    expect(screen.queryByText('Rev 2')).not.toBeInTheDocument()
  })

  it('still fetches the history for a single-revision document', async () => {
    // Hiding is a display decision. The revision API and everything behind it
    // are untouched, so the request still goes out.
    await renderWith(1)
    expect(vi.mocked(fetch).mock.calls.some((call) =>
      String(call[0]).includes('/revisions'))).toBe(true)
  })

  it('surfaces a history error instead of silently hiding it', async () => {
    vi.stubGlobal('fetch', mockFetch({
      ...NO_CHAT_SESSIONS,
      '/api/v1/documents/doc-1/revisions': () =>
        errorResponse('INTERNAL_ERROR', '버전 이력을 불러오지 못했습니다.', 500),
      '/api/v1/documents/doc-1': makeDetail(),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    expect(await screen.findByText('버전 이력을 불러오지 못했습니다.')).toBeInTheDocument()
  })
})

describe('DocumentPage text preview', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  function stub(text: unknown, detail = makeDetail()) {
    vi.stubGlobal('fetch', mockFetch({
      ...NO_CHAT_SESSIONS,
      // Ahead of the bare document route, which would otherwise swallow it.
      '/api/v1/documents/doc-1/text': text,
      '/api/v1/documents/doc-1/revisions': revisionsResponse(),
      '/api/v1/documents/doc-1': detail,
    }))
  }

  function textCalls() {
    return vi.mocked(fetch).mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.includes('/text'))
  }

  it('asks for nothing until the reader opens it', async () => {
    // A long report is hundreds of thousands of characters; it is not fetched
    // for every visit to the page.
    stub(makeTextPage())
    renderAt(<DocumentPage />, PATH, ROUTE)
    await screen.findByRole('heading', { name: makeDetail().title })

    expect(screen.getByRole('button', { name: /원문 텍스트 보기/ }))
      .toHaveAttribute('aria-expanded', 'false')
    expect(textCalls()).toEqual([])
  })

  it('loads a bounded first page and says it is extracted text', async () => {
    stub(makeTextPage({ items: [makeTextBlock({ text: '총 사업비는 3억 원이다.' })], total: 120, has_more: true }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    await userEvent.click(await screen.findByRole('button', { name: /원문 텍스트 보기/ }))

    expect(await screen.findByText('총 사업비는 3억 원이다.')).toBeInTheDocument()
    // Named for what it is, so nobody reads a missing table as a missing
    // section of the document.
    expect(screen.getByText(/추출한 텍스트/)).toBeInTheDocument()
    expect(textCalls()).toEqual(['/api/v1/documents/doc-1/text?offset=0&limit=20'])
  })

  it('appends the next page rather than replacing what is on screen', async () => {
    let page = 0
    stub(() => jsonResponse(page++ === 0
      ? makeTextPage({ items: [makeTextBlock({ text: '첫 번째 문단' })], total: 2, has_more: true })
      : makeTextPage({ items: [makeTextBlock({ chunk_index: 1, text: '두 번째 문단' })], offset: 1, total: 2, has_more: false })))
    renderAt(<DocumentPage />, PATH, ROUTE)

    await userEvent.click(await screen.findByRole('button', { name: /원문 텍스트 보기/ }))
    await screen.findByText('첫 번째 문단')
    await userEvent.click(screen.getByRole('button', { name: '더 보기' }))

    expect(await screen.findByText('두 번째 문단')).toBeInTheDocument()
    // Reading continues instead of restarting.
    expect(screen.getByText('첫 번째 문단')).toBeInTheDocument()
    expect(textCalls()[1]).toBe('/api/v1/documents/doc-1/text?offset=1&limit=20')
    // Nothing left to fetch, so no button offering to fetch it.
    expect(screen.queryByRole('button', { name: '더 보기' })).not.toBeInTheDocument()
  })

  it('does not re-fetch when collapsed and reopened', async () => {
    stub(makeTextPage())
    renderAt(<DocumentPage />, PATH, ROUTE)
    const toggle = await screen.findByRole('button', { name: /원문 텍스트 보기/ })

    await userEvent.click(toggle)
    await screen.findByText(makeTextBlock().text)
    await userEvent.click(toggle)
    await userEvent.click(toggle)

    expect(await screen.findByText(makeTextBlock().text)).toBeInTheDocument()
    expect(textCalls()).toHaveLength(1)
  })

  it('reports a refusal instead of rendering an empty box', async () => {
    stub(() => errorResponse('DOCUMENT_NOT_FOUND', '문서를 찾을 수 없습니다.', 404))
    renderAt(<DocumentPage />, PATH, ROUTE)
    await userEvent.click(await screen.findByRole('button', { name: /원문 텍스트 보기/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('문서를 찾을 수 없습니다.')
  })

  it('is not offered for a document with no searchable body', async () => {
    // Nothing was extracted, so there is nothing to preview.
    stub(makeTextPage(), makeDetail({ is_searchable: false }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    await screen.findByRole('heading', { name: makeDetail().title })
    expect(screen.queryByRole('button', { name: /원문 텍스트 보기/ })).not.toBeInTheDocument()
  })

  it('keeps the body out of the detail response', async () => {
    // The preview is a separate, paged request precisely so the detail
    // payload stays small. If the body ever rides along in the detail
    // response, this is where it shows up.
    stub(makeTextPage())
    renderAt(<DocumentPage />, PATH, ROUTE)
    await screen.findByRole('heading', { name: makeDetail().title })

    expect(Object.keys(makeDetail())).not.toContain('text')
    expect(Object.keys(makeDetail())).not.toContain('extracted_text')
    expect(screen.queryByText(makeTextBlock().text)).not.toBeInTheDocument()
  })
})

describe('DocumentPage dates', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.stubGlobal('URL', Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:doc-1'),
      revokeObjectURL: vi.fn(),
    }))
  })
  afterEach(() => vi.unstubAllGlobals())

  async function renderDetail(overrides = {}) {
    vi.stubGlobal('fetch', mockFetch({
      ...NO_CHAT_SESSIONS,
      '/api/v1/documents/doc-1/revisions': revisionsResponse(),
      '/api/v1/documents/doc-1': makeDetail(overrides),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    await screen.findByRole('heading', { name: makeDetail().title })
  }

  it('shows three distinct dates, each labelled for what it is', async () => {
    await renderDetail()
    for (const label of ['문서 작성일', '시스템 등록일', '원본 파일 수정일']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
  })

  it('shows the date the document itself states', async () => {
    await renderDetail()
    expect(screen.getByText('2025. 11. 26.')).toBeInTheDocument()
  })

  it('says 알 수 없음 rather than inventing a day', async () => {
    // A cover stating only "2026년도" gives a year and no date. Rendering
    // 2026. 1. 1. would be the UI asserting something the document does not.
    await renderDetail({ document_date: null })
    expect(screen.getByText('알 수 없음')).toBeInTheDocument()
    expect(screen.queryByText(/2026\. 1\. 1\./)).not.toBeInTheDocument()
  })

  it('shows the file mtime as 원본 파일 수정일, not the row bookkeeping timestamp', async () => {
    // updated_at moves when a revision is promoted -- something no reader did
    // and none would recognise as the document being modified.
    await renderDetail({
      source_modified_at: '2025-12-01T09:30:00Z',
      updated_at: '2026-08-30T04:12:00Z',
    })
    const modified = screen.getByText('원본 파일 수정일').closest('div')!
    expect(within(modified).getByText('2025. 12. 01.')).toBeInTheDocument()
    expect(within(modified).queryByText('2026. 08. 30.')).not.toBeInTheDocument()
  })

  it('falls back when the file has no recorded mtime', async () => {
    await renderDetail({ source_modified_at: null, document_date: '2025-11-26' })
    const modified = screen.getByText('원본 파일 수정일').closest('div')!
    expect(within(modified).getByText('알 수 없음')).toBeInTheDocument()
  })
})
