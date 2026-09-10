import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DocumentChat } from './DocumentChat'
import {
  errorResponse,
  jsonResponse,
  makeAssistantMessage,
  makeSession,
  makeSessionDetail,
  makeUserMessage,
  mockFetch,
  renderAt,
} from '../test/helpers'

const DOCUMENT_ID = 'doc-1'
const TITLE = '서버 장애 대응 지침'

function bodyOf(call: unknown[]): Record<string, unknown> {
  const init = call[1] as RequestInit
  return JSON.parse(String(init.body))
}

function postsTo(fetchMock: ReturnType<typeof vi.fn>, path: string) {
  return fetchMock.mock.calls.filter(
    (call) => String(call[0]).includes(path) && (call[1] as RequestInit)?.method === 'POST',
  )
}

function render(fetchMock: ReturnType<typeof vi.fn>, available = true) {
  vi.stubGlobal('fetch', fetchMock)
  return renderAt(
    <DocumentChat documentId={DOCUMENT_ID} title={TITLE} available={available} />,
    '/documents/doc-1',
    '/documents/:documentId',
  )
}

describe('DocumentChat', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))
  afterEach(() => vi.unstubAllGlobals())

  it('creates no session until a question is actually asked', async () => {
    const fetchMock = mockFetch({
      '/api/v1/chat/sessions': { items: [], page: 1, size: 20, total: 0 },
    })
    render(fetchMock)
    await screen.findByText(/아직 주고받은 질문이 없습니다/)
    // Otherwise every visit to every document leaves an empty session behind.
    expect(postsTo(fetchMock, '/chat/sessions')).toHaveLength(0)
  })

  it('binds a new session to this document at creation', async () => {
    const fetchMock = mockFetch({
      '/api/v1/chat/sessions/ses-9/messages': () =>
        jsonResponse({
          message_id: 'm1', answer: '답변', refused: false, sources: [],
          created_at: '2026-09-01T00:00:00Z',
        }, 201),
      '/api/v1/chat/sessions/ses-9': makeSessionDetail(
        [makeUserMessage(), makeAssistantMessage()],
        { session_id: 'ses-9' },
      ),
      '/api/v1/chat/sessions': (() => {
        let created = false
        return () => {
          if (!created) {
            created = true
            return jsonResponse({ items: [], page: 1, size: 20, total: 0 })
          }
          return jsonResponse(makeSession({ session_id: 'ses-9' }), 201)
        }
      })(),
    })
    render(fetchMock)
    await screen.findByText(/아직 주고받은 질문이 없습니다/)

    await userEvent.type(screen.getByRole('textbox'), '대응 절차가 뭐야?')
    await userEvent.click(screen.getByRole('button', { name: '전송' }))

    await waitFor(() => expect(postsTo(fetchMock, '/chat/sessions')).not.toHaveLength(0))
    const created = postsTo(fetchMock, '/chat/sessions').find(
      (call) => !String(call[0]).includes('/messages'),
    )
    expect(bodyOf(created!)).toMatchObject({ document_id: DOCUMENT_ID })
  })

  it('sends no document with the message itself', async () => {
    const fetchMock = mockFetch({
      '/api/v1/chat/sessions/ses-1/messages': () =>
        jsonResponse({
          message_id: 'm1', answer: '답변', refused: false, sources: [],
          created_at: '2026-09-01T00:00:00Z',
        }, 201),
      '/api/v1/chat/sessions/ses-1': makeSessionDetail(),
      '/api/v1/chat/sessions': {
        items: [makeSession({
          session_id: 'ses-1',
          document_scope: { document_id: DOCUMENT_ID, accessible: true, title: TITLE },
        })],
        page: 1, size: 20, total: 1,
      },
    })
    render(fetchMock)
    await screen.findByText('사업 예산이 얼마야?')

    await userEvent.type(screen.getByRole('textbox'), '추가 질문')
    await userEvent.click(screen.getByRole('button', { name: '전송' }))

    await waitFor(() => expect(postsTo(fetchMock, '/messages')).toHaveLength(1))
    // The scope lives on the session. A document in the message body would be
    // a second, client-controlled source of truth for it.
    expect(bodyOf(postsTo(fetchMock, '/messages')[0])).toEqual({ message: '추가 질문' })
  })

  it('continues the existing session for this document instead of starting another', async () => {
    const fetchMock = mockFetch({
      '/api/v1/chat/sessions/ses-1': makeSessionDetail(),
      '/api/v1/chat/sessions': {
        items: [
          makeSession({ session_id: 'ses-other', document_scope: null }),
          makeSession({
            session_id: 'ses-1',
            document_scope: { document_id: DOCUMENT_ID, accessible: true, title: TITLE },
          }),
        ],
        page: 1, size: 20, total: 2,
      },
    })
    render(fetchMock)
    expect(await screen.findByText('사업 예산이 얼마야?')).toBeInTheDocument()
    expect(postsTo(fetchMock, '/chat/sessions')).toHaveLength(0)
  })

  it('ignores a session scoped to a different document', async () => {
    const fetchMock = mockFetch({
      '/api/v1/chat/sessions': {
        items: [makeSession({
          session_id: 'ses-2',
          document_scope: { document_id: 'doc-99', accessible: true, title: '다른 문서' },
        })],
        page: 1, size: 20, total: 1,
      },
    })
    render(fetchMock)
    await screen.findByText(/아직 주고받은 질문이 없습니다/)
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes('ses-2'))).toBe(false)
  })

  it('keeps a failed question on screen and shows the server message', async () => {
    const fetchMock = mockFetch({
      '/api/v1/chat/sessions/ses-1/messages': () =>
        errorResponse('DOCUMENT_NOT_FOUND', '문서를 찾을 수 없습니다.', 404),
      '/api/v1/chat/sessions/ses-1': makeSessionDetail([]),
      '/api/v1/chat/sessions': {
        items: [makeSession({
          session_id: 'ses-1',
          document_scope: { document_id: DOCUMENT_ID, accessible: true, title: TITLE },
        })],
        page: 1, size: 20, total: 1,
      },
    })
    render(fetchMock)
    await screen.findByRole('textbox')

    await userEvent.type(screen.getByRole('textbox'), '권한이 사라진 문서 질문')
    await userEvent.click(screen.getByRole('button', { name: '전송' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('문서를 찾을 수 없습니다.')
    // Neither "sent" nor "not sent" is known, so the turn stays on screen and
    // is labelled as failed rather than being silently dropped or claimed.
    expect(
      screen.getByText('권한이 사라진 문서 질문', { selector: '.chat-text' }),
    ).toBeInTheDocument()
    expect(screen.getByText('전송 실패')).toBeInTheDocument()
    // The draft is kept too, so the question can be retried without retyping.
    expect(screen.getByRole('textbox')).toHaveValue('권한이 사라진 문서 질문')
  })

  it('tells the reader the answers come from this document only', async () => {
    const fetchMock = mockFetch({
      '/api/v1/chat/sessions': { items: [], page: 1, size: 20, total: 0 },
    })
    render(fetchMock)
    expect(await screen.findByText(/다른 문서는 참고하지 않습니다/)).toBeInTheDocument()
  })

  describe('when the deployment cannot call a provider', () => {
    const NO_SESSIONS = {
      '/api/v1/chat/sessions': { items: [], page: 1, size: 20, total: 0 },
    }

    it('says so instead of leaving the reader to find out by asking', async () => {
      render(mockFetch(NO_SESSIONS), false)
      expect(
        await screen.findByText('AI 질문 기능이 현재 비활성화되어 있습니다.'),
      ).toBeInTheDocument()
      expect(screen.queryByText(/다른 문서는 참고하지 않습니다/)).not.toBeInTheDocument()
    })

    it('disables the input and the send button', async () => {
      render(mockFetch(NO_SESSIONS), false)
      expect(await screen.findByRole('textbox')).toBeDisabled()
      expect(screen.getByRole('button', { name: '전송' })).toBeDisabled()
    })

    it('sends nothing even if a submit is forced through', async () => {
      const fetchMock = mockFetch(NO_SESSIONS)
      render(fetchMock, false)
      await screen.findByRole('textbox')
      // The disabled control is a convenience; the guard in onSend is what
      // makes "no request leaves" true.
      await userEvent.click(screen.getByRole('button', { name: '전송' }))
      expect(postsTo(fetchMock, '/chat/sessions')).toHaveLength(0)
      expect(postsTo(fetchMock, '/messages')).toHaveLength(0)
    })
  })
})
