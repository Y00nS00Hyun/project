import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from '../auth/AuthContext'
import type {
  AccessibleSource,
  AssistantChatMessage,
  ChatMessage,
  ChatSessionDetail,
  ChatSessionSummary,
  DocumentDetail,
  PlainChatMessage,
  Revision,
  SearchItem,
  SearchResponse,
} from '../api/types'

/**
 * Render one page at one route.
 *
 * Wrapped in AuthProvider because the nav strip reads the signed-in user from
 * it. The provider asks the server who that is, so a test that renders a page
 * with a nav wants SIGNED_IN among its stubbed routes -- without it the answer
 * is simply "nobody", which is a valid state and not an error.
 *
 * Note this does NOT apply the route guard: these tests render a page
 * directly, which is what lets them stay about the page. Guard behaviour is
 * tested where it lives, in App.test.tsx and RequireAuth.test.tsx.
 */
export function renderAt(ui: ReactElement, path = '/search', routePath = '/search') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <Routes>
          <Route path={routePath} element={ui} />
        </Routes>
      </AuthProvider>
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

/**
 * The document page asks for the caller's sessions to find an existing
 * conversation about the document being viewed. Tests that are not about chat
 * still have to answer that request: an unstubbed route renders an error
 * panel, which would then be indistinguishable from the error the test is
 * actually asserting on.
 */
/**
 * A signed-in session, for tests that render anything carrying the nav strip
 * or that go through the route guard.
 */
export const SIGNED_IN = {
  '/api/v1/auth/me': {
    user_id: 'user-1',
    name: '윤수현',
    is_system_admin: false,
  },
  '/api/v1/auth/capability': { local_auth_enabled: true, signup_enabled: true },
}

/** The same, for an administrator. */
export const SIGNED_IN_ADMIN = {
  ...SIGNED_IN,
  '/api/v1/auth/me': { ...SIGNED_IN['/api/v1/auth/me'], is_system_admin: true },
}

export const NO_CHAT_SESSIONS = {
  '/api/v1/chat/sessions': { items: [], page: 1, size: 20, total: 0 },
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

/** GET /folders returns canonical paths and display names separately. */
export const FOLDERS = [
  { path: '프로젝트_A', name: '프로젝트_A', parent_path: null, depth: 1, document_count: 3 },
  { path: '프로젝트_A/요구사항', name: '요구사항', parent_path: '프로젝트_A', depth: 2, document_count: 2 },
  { path: '프로젝트_A/완료', name: '완료', parent_path: '프로젝트_A', depth: 2, document_count: 1 },
  // Legacy folder: the canonical path is escaped, the name is readable.
  { path: '%C7%C1%B7%CE%C1%A7Ʈ_B', name: '프로젝트_B', parent_path: null, depth: 1, document_count: 2 },
]

export const emptyMetadata = {
  '/api/v1/folders': { items: FOLDERS },
  '/api/v1/departments': { items: [{ id: 'dep-1', name: '기획조정실' }] },
  // The real GET /tags returns document kinds and free-form tags together;
  // the namespace prefix is what separates them.
  '/api/v1/tags': {
    items: [{ id: 12, name: '보안' }, { id: 1, name: '종류:매뉴얼' }, { id: 4, name: '종류:보고서' }],
    page: 1,
    size: 100,
    total: 3,
  },
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
    // The common case: generation is on and this revision has been summarized.
    // Tests that care about a different state override the whole object.
    summary: {
      state: 'SUCCESS',
      reason: null,
      content: '2026년 AI 문서관리 사업의 배경과 예산 계획을 정리한 문서입니다.',
      generated_at: '2026-08-30T05:00:00Z',
      available: true,
      revision_id: 'rev-2',
    },
    // Both document-text features share one capability, so the fixture does too.
    chat: { available: true },
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

// --- chat fixtures ---------------------------------------------------------

export function makeSession(overrides: Partial<ChatSessionSummary> = {}): ChatSessionSummary {
  return {
    session_id: 'ses-1',
    title: '2026년 사업계획',
    created_at: '2026-09-01T01:00:00Z',
    updated_at: '2026-09-01T02:00:00Z',
    message_count: 2,
    // An ordinary whole-corpus session. Scoped ones pass document_scope.
    document_scope: null,
    ...overrides,
  }
}

export function makeUserMessage(overrides: Partial<PlainChatMessage> = {}): PlainChatMessage {
  return {
    message_id: 'msg-user-1',
    role: 'user',
    content: '사업 예산이 얼마야?',
    created_at: '2026-09-01T02:00:00Z',
    ...overrides,
  }
}

export function makeAccessibleSource(overrides: Partial<AccessibleSource> = {}): AccessibleSource {
  return {
    document_id: 'doc-1',
    revision_id: 'rev-4',
    chunk_id: 'chunk-1',
    title: '2026년 사업계획서',
    file_type: 'hwpx',
    section_title: null,
    anchor: { type: 'paragraph', paragraph_index: 32, paragraph_end: 34 },
    accessible: true,
    ...overrides,
  }
}

export function makeAssistantMessage(
  overrides: Partial<AssistantChatMessage> = {},
): AssistantChatMessage {
  return {
    message_id: 'msg-ai-1',
    role: 'assistant',
    content: '총 사업비는 3억 원입니다.',
    refused: false,
    has_inaccessible_sources: false,
    content_hidden: false,
    sources: [makeAccessibleSource()],
    created_at: '2026-09-01T02:00:05Z',
    ...overrides,
  }
}

export function makeSessionDetail(
  messages: ChatMessage[] = [makeUserMessage(), makeAssistantMessage()],
  overrides: Partial<ChatSessionDetail> = {},
): ChatSessionDetail {
  return {
    session_id: 'ses-1',
    title: '2026년 사업계획',
    created_at: '2026-09-01T01:00:00Z',
    updated_at: '2026-09-01T02:00:00Z',
    document_scope: null,
    messages: { items: messages, page: 1, size: 50, total: messages.length },
    ...overrides,
  }
}
