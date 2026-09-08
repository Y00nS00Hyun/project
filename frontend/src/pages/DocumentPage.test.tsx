import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DocumentPage } from './DocumentPage'
import {
  errorResponse,
  jsonResponse,
  makeDetail,
  makeRevision,
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
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    expect(await screen.findByRole('heading', { name: '2026년 AI 문서관리 사업계획서' })).toBeInTheDocument()
    expect(screen.getByText('기획조정실')).toBeInTheDocument()
    expect(screen.getByText('HWPX')).toBeInTheDocument()
    expect(screen.getByText('보안')).toBeInTheDocument()
  })

  it('keeps the served revision and the newest revision apart', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
        '/api/v1/documents/doc-1/revisions': revisionsResponse(),
        '/api/v1/documents/doc-1': makeDetail(),
      }),
    )
    renderAt(<DocumentPage />, PATH, ROUTE)

    // Rev 2 is what search serves; Rev 3 exists on disk but is not READY. The
    // two fields are read off their own <dt> so the assertion cannot be
    // satisfied by the revision list happening to mention the same text.
    const served = await screen.findByText('현재 검색 버전', { selector: 'dt' })
    expect(served.nextElementSibling).toHaveTextContent('Rev 2')

    const newest = screen.getByText('최신 파일 버전', { selector: 'dt' })
    expect(newest.nextElementSibling).toHaveTextContent('Rev 3')
    expect(
      screen.getByText(/최신 파일 버전이 아직 검색에 반영되지 않았습니다/),
    ).toBeInTheDocument()
  })

  it('renders version history with localised, unmerged statuses', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch({
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
      '/api/v1/documents/doc-1/revisions': () => jsonResponse({
        items: [makeRevision({ revision_no: ++page === 1 ? 21 : 1 })],
        page, size: 20, total: 21,
      }),
      '/api/v1/documents/doc-1': makeDetail(),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    await screen.findByText('Rev 21')
    await userEvent.click(screen.getByRole('button', { name: '다음' }))
    expect(await screen.findByText('Rev 1')).toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.some((call) =>
      String(call[0]).endsWith('/revisions?page=2&size=20'))).toBe(true)
    expect(screen.queryByText('Rev 21')).not.toBeInTheDocument()
  })

  it('uses the login state when authentication expires during download', async () => {
    vi.stubGlobal('fetch', mockFetch({
      '/api/v1/documents/doc-1/download': () => errorResponse('UNAUTHENTICATED', '인증 만료', 401),
      '/api/v1/documents/doc-1/revisions': revisionsResponse(),
      '/api/v1/documents/doc-1': makeDetail(),
    }))
    renderAt(<DocumentPage />, PATH, ROUTE)
    await userEvent.click(await screen.findByRole('button', { name: '원본 다운로드' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('로그인이 필요합니다.')
  })
})
