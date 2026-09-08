import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type {
  DocumentDetail,
  Revision,
  SearchItem,
  SearchResponse,
} from '../api/types'

export function renderAt(ui: ReactElement, path = '/search', routePath = '/search') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={routePath} element={ui} />
      </Routes>
    </MemoryRouter>,
  )
}

/** Build a fetch stub that answers by URL path. */
export function mockFetch(
  routes: Record<string, unknown | (() => Response | Promise<Response>)>,
) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.signal?.aborted) throw new DOMException('aborted', 'AbortError')
    const key = Object.keys(routes).find((route) => url.startsWith(route))
    if (key === undefined) {
      return jsonResponse({ error: { code: 'INTERNAL_ERROR', message: `no stub for ${url}`, request_id: 'x' } }, 500)
    }
    const handler = routes[key]
    if (typeof handler === 'function') return (handler as () => Response)()
    return jsonResponse(handler)
  })
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-Id': 'req-test-1' },
  })
}

export function errorResponse(code: string, message: string, status: number): Response {
  return jsonResponse({ error: { code, message, request_id: 'req-test-1' } }, status)
}

export const emptyMetadata = {
  '/api/v1/departments': { items: [{ id: 'dep-1', name: '기획조정실' }] },
  '/api/v1/tags': { items: [{ id: 12, name: '보안' }], page: 1, size: 100, total: 1 },
}

export function makeItem(overrides: Partial<SearchItem> = {}): SearchItem {
  return {
    document_id: 'doc-1',
    title: '2026년 AI 문서관리 사업계획서',
    file_type: 'hwpx',
    department: { id: 'dep-1', name: '기획조정실' },
    tags: [{ id: 12, name: '보안' }],
    updated_at: '2026-08-30T04:12:00Z',
    current_revision: { revision_id: 'rev-4', revision_no: 4, created_at: '2026-08-30T04:12:00Z' },
    has_newer_revision: false,
    snippet: '총 사업비는 300,000,000원이며',
    matched_chunk: {
      chunk_id: 'chunk-1',
      revision_id: 'rev-4',
      section_title: null,
      anchor: { type: 'paragraph', paragraph_index: 42, paragraph_end: null },
    },
    ...overrides,
  }
}

export function makeSearchResponse(
  items: SearchItem[],
  overrides: Partial<SearchResponse> = {},
): SearchResponse {
  return { items, page: 1, size: 20, total: items.length, ...overrides }
}

export function makeDetail(overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    document_id: 'doc-1',
    title: '2026년 AI 문서관리 사업계획서',
    file_type: 'hwpx',
    department: { id: 'dep-1', name: '기획조정실' },
    owner: null,
    tags: [{ id: 12, name: '보안' }],
    created_at: '2026-01-05T00:00:00Z',
    updated_at: '2026-08-30T04:12:00Z',
    current_revision: { revision_id: 'rev-2', revision_no: 2, created_at: '2026-08-30T04:12:00Z' },
    latest_revision: { revision_id: 'rev-3', revision_no: 3, created_at: '2026-09-01T00:00:00Z' },
    is_searchable: true,
    downloadable: true,
    ...overrides,
  }
}

export function makeRevision(overrides: Partial<Revision> = {}): Revision {
  return {
    revision_id: 'rev-2',
    revision_no: 2,
    content_hash: 'a'.repeat(64),
    file_size: 20480,
    source_modified_at: '2026-08-30T04:00:00Z',
    parse_status: 'SUCCESS',
    parse_result_code: 'TEXT_EXTRACTED',
    is_current: true,
    is_ready: true,
    created_at: '2026-08-30T04:12:00Z',
    ...overrides,
  }
}
