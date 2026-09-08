import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import {
  ApiClientError,
  buildQuery,
  filenameFromDisposition,
  getFile,
  getJson,
} from './client'
import { searchDocuments } from './search'
import { errorResponse, jsonResponse } from '../test/helpers'

describe('buildQuery', () => {
  it('omits null, undefined and empty values instead of sending blanks', () => {
    expect(buildQuery({ q: '', page: undefined, year: null, size: 20 })).toBe('?size=20')
  })

  it('repeats a key for array values so tag_id stays AND-combined', () => {
    // Comma-joining would make the backend see one tag named "12,13".
    expect(buildQuery({ tag_id: [12, 13] })).toBe('?tag_id=12&tag_id=13')
  })

  it('returns an empty string when nothing is set', () => {
    expect(buildQuery({})).toBe('')
  })
})

describe('filenameFromDisposition', () => {
  it('prefers the RFC 5987 encoded form for Korean filenames', () => {
    const header = `attachment; filename="doc.hwpx"; filename*=UTF-8''${encodeURIComponent('사업계획서.hwpx')}`
    expect(filenameFromDisposition(header, 'fallback')).toBe('사업계획서.hwpx')
  })

  it('falls back to the plain form, then to the supplied default', () => {
    expect(filenameFromDisposition('attachment; filename="a.pdf"', 'fallback')).toBe('a.pdf')
    expect(filenameFromDisposition(null, 'fallback')).toBe('fallback')
  })
})

describe('error envelope', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => vi.unstubAllGlobals())

  it('turns the contract envelope into an ApiClientError with code and request id', async () => {
    vi.mocked(fetch).mockResolvedValue(
      errorResponse('DOCUMENT_NOT_FOUND', '문서를 찾을 수 없습니다.', 404),
    )
    const error = await getJson('/documents/x').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiClientError)
    const apiError = error as ApiClientError
    expect(apiError.code).toBe('DOCUMENT_NOT_FOUND')
    expect(apiError.message).toBe('문서를 찾을 수 없습니다.')
    expect(apiError.requestId).toBe('req-test-1')
    expect(apiError.isNotFound).toBe(true)
  })

  it('does not surface a non-JSON body to the caller', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response('<html>nginx 502 /srv/shared/docs</html>', { status: 502 }),
    )
    const error = (await getJson('/search').catch((e: unknown) => e)) as ApiClientError
    expect(error.code).toBe('INTERNAL_ERROR')
    expect(error.message).not.toContain('nginx')
    expect(error.message).not.toContain('/srv')
  })

  it('reports a transport failure without leaking the underlying reason', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('Failed to fetch http://internal:8000'))
    const error = (await getJson('/search').catch((e: unknown) => e)) as ApiClientError
    expect(error.code).toBe('NETWORK_ERROR')
    expect(error.message).toBe('서버에 연결할 수 없습니다.')
  })

  it('propagates an abort so a stale request can be discarded', async () => {
    const controller = new AbortController()
    vi.mocked(fetch).mockRejectedValue(new DOMException('aborted', 'AbortError'))
    const error = await getJson('/search', { signal: controller.signal }).catch((e: unknown) => e)
    expect((error as DOMException).name).toBe('AbortError')
  })
})

describe('request shape', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it('omits q entirely when the query is blank, so the backend browses', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ items: [], page: 1, size: 20, total: 0 }))
    await searchDocuments({ q: '   ', page: 1 })
    const url = String(vi.mocked(fetch).mock.calls[0][0])
    expect(url).not.toContain('q=')
    expect(url).toContain('page=1')
  })

  it('never sends a mode parameter: the server picks the retrieval route', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ items: [], page: 1, size: 20, total: 0 }))
    await searchDocuments({ q: '사업계획', departmentId: 'dep-1', year: 2026, tagIds: [12, 13], fileType: 'hwpx' })
    const url = String(vi.mocked(fetch).mock.calls[0][0])
    expect(url).not.toContain('mode=')
    expect(url).toContain('department_id=dep-1')
    expect(url).toContain('year=2026')
    expect(url).toContain('tag_id=12&tag_id=13')
    expect(url).toContain('file_type=hwpx')
  })

  it('attaches the development identity header from config, never from user input', async () => {
    vi.stubEnv('VITE_DEBUG_USER_ID', '11111111-1111-1111-1111-111111111111')
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ items: [], page: 1, size: 20, total: 0 }))
    await searchDocuments({})
    const init = vi.mocked(fetch).mock.calls[0][1] as RequestInit
    const headers = init.headers as Record<string, string>
    expect(headers['X-Debug-User-Id']).toBe('11111111-1111-1111-1111-111111111111')
    vi.unstubAllEnvs()
  })

  it('sends no identity header when none is configured', async () => {
    vi.stubEnv('VITE_DEBUG_USER_ID', '')
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ items: [], page: 1, size: 20, total: 0 }))
    await searchDocuments({})
    const init = vi.mocked(fetch).mock.calls[0][1] as RequestInit
    expect(Object.keys(init.headers as Record<string, string>)).not.toContain('X-Debug-User-Id')
    vi.unstubAllEnvs()
  })

  it('ignores a configured debug identity in production', async () => {
    vi.stubEnv('DEV', false)
    vi.stubEnv('VITE_DEBUG_USER_ID', 'development-only-marker')
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    try {
      await searchDocuments({})
      const headers = vi.mocked(fetch).mock.calls[0][1]?.headers as Record<string, string>
      expect(headers['X-Debug-User-Id']).toBeUndefined()
    } finally {
      vi.unstubAllEnvs()
    }
  })
})

describe('getFile', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it('returns bytes and a display filename, and nothing resembling a path', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response('bytes', {
        status: 200,
        headers: { 'Content-Disposition': 'attachment; filename="plan.hwpx"' },
      }),
    )
    const file = await getFile('/documents/doc-1/download', 'fallback')
    expect(file.filename).toBe('plan.hwpx')
    expect(file.filename).not.toContain('/')
    // jsdom's Blob has no text(); size is enough to prove the bytes came through.
    expect(file.blob.size).toBe(5)
  })
})
