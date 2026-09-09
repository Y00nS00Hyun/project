import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ChatPage } from './ChatPage'
import { DocumentPage } from './DocumentPage'
import {
  errorResponse,
  jsonResponse,
  makeAccessibleSource,
  makeAssistantMessage,
  makeSession,
  makeSessionDetail,
  makeUserMessage,
} from '../test/helpers'
import type { ChatSessionSummary } from '../api/types'

/**
 * Route by method as well as path.
 *
 * Creating a session and listing sessions are the same URL and differ only by
 * verb, so a path-prefix stub would silently answer the wrong one.
 */
type Stub = () => Response | Promise<Response>

interface ChatStubs {
  sessions?: Stub
  create?: Stub
  detail?: Stub
  send?: Stub
}

interface Call {
  method: string
  url: string
  body: unknown
}

function stubChat(stubs: ChatStubs) {
  const calls: Call[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (init?.signal?.aborted) throw new DOMException('aborted', 'AbortError')
    const path = url.split('?')[0]

    if (method === 'POST' && path.endsWith('/messages')) {
      return stubs.send?.() ?? errorResponse('INTERNAL_ERROR', 'no send stub', 500)
    }
    if (method === 'POST' && path === '/api/v1/chat/sessions') {
      return stubs.create?.() ?? jsonResponse(makeSession({ session_id: 'ses-new', title: null }))
    }
    if (method === 'GET' && path === '/api/v1/chat/sessions') {
      return stubs.sessions?.() ?? jsonResponse(page([makeSession()]))
    }
    if (method === 'GET' && path.startsWith('/api/v1/chat/sessions/')) {
      return stubs.detail?.() ?? jsonResponse(makeSessionDetail())
    }
    if (method === 'GET' && path.startsWith('/api/v1/documents/')) {
      return errorResponse('DOCUMENT_NOT_FOUND', '문서를 찾을 수 없습니다.', 404)
    }
    return errorResponse('INTERNAL_ERROR', `no stub for ${method} ${url}`, 500)
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

function page(items: ChatSessionSummary[]) {
  return { items, page: 1, size: 20, total: items.length }
}

function renderChat(path = '/chat/ses-1') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:sessionId" element={<ChatPage />} />
        <Route path="/documents/:documentId" element={<DocumentPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** A response that resolves only when the test releases it. */
function deferred() {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  return { gate, release: () => release() }
}

describe('ChatPage · session list', () => {
  it('renders the sessions the API returned', async () => {
    stubChat({
      sessions: () =>
        jsonResponse(
          page([
            makeSession(),
            makeSession({ session_id: 'ses-2', title: null, message_count: 7 }),
          ]),
        ),
    })
    renderChat('/chat')

    const list = await screen.findByRole('navigation', { name: '대화 목록' })
    expect(within(list).getByText('2026년 사업계획')).toBeInTheDocument()
    // title is nullable in the contract and is never generated from a question.
    expect(within(list).getByText('제목 없는 대화')).toBeInTheDocument()
    expect(within(list).getByText('메시지 7개')).toBeInTheDocument()
  })

  it('shows an empty state when there are no sessions', async () => {
    stubChat({ sessions: () => jsonResponse(page([])) })
    renderChat('/chat')

    expect(await screen.findByText('아직 대화가 없습니다.')).toBeInTheDocument()
  })

  it('prompts to pick a session when none is selected', async () => {
    stubChat({})
    renderChat('/chat')

    expect(
      await screen.findByText('왼쪽에서 대화를 선택하거나 새 대화를 시작해 주세요.'),
    ).toBeInTheDocument()
  })

  it('never sends a user id and offers no field to type one', async () => {
    const calls = stubChat({})
    renderChat('/chat')
    await screen.findByRole('navigation', { name: '대화 목록' })

    expect(screen.queryByLabelText(/사용자 ?ID/i)).not.toBeInTheDocument()
    for (const call of calls) {
      expect(call.url).not.toMatch(/user_id|department_id/)
      expect(JSON.stringify(call.body ?? {})).not.toMatch(/user_id|department_id/)
    }
  })
})

describe('ChatPage · create session', () => {
  it('posts the contract minimum and navigates to the new session', async () => {
    const calls = stubChat({
      create: () => jsonResponse({ ...makeSession({ session_id: 'ses-new', title: null }) }),
      detail: () => jsonResponse(makeSessionDetail([], { session_id: 'ses-new', title: null })),
    })
    renderChat('/chat')

    await userEvent.click(await screen.findByRole('button', { name: '+ 새 대화' }))

    await waitFor(() =>
      expect(screen.getByText('문서에 대해 궁금한 내용을 질문해 보세요.')).toBeInTheDocument(),
    )
    const create = calls.find((call) => call.method === 'POST')
    expect(create?.url).toContain('/api/v1/chat/sessions')
    // title is optional; the minimum allowed body is an empty object.
    expect(create?.body).toEqual({})
  })
})

describe('ChatPage · transcript', () => {
  it('renders user and assistant turns in the order the server gave', async () => {
    stubChat({})
    renderChat()

    expect(await screen.findByText('사업 예산이 얼마야?')).toBeInTheDocument()
    expect(screen.getByText('총 사업비는 3억 원입니다.')).toBeInTheDocument()
    const roles = screen.getAllByText(/^(사용자|AI)$/).map((node) => node.textContent)
    expect(roles).toEqual(['사용자', 'AI'])
  })

  it('shows an empty state for a session with no messages', async () => {
    stubChat({ detail: () => jsonResponse(makeSessionDetail([])) })
    renderChat()

    expect(
      await screen.findByText('문서에 대해 궁금한 내용을 질문해 보세요.'),
    ).toBeInTheDocument()
  })

  it('renders a refusal as a note, not as an error', async () => {
    stubChat({
      detail: () =>
        jsonResponse(
          makeSessionDetail([
            makeUserMessage(),
            makeAssistantMessage({
              content: '관련 문서에서 확인할 수 없습니다.',
              refused: true,
              sources: [],
            }),
          ]),
        ),
    })
    renderChat()

    // The server's own answer text comes first.
    expect(await screen.findByText('관련 문서에서 확인할 수 없습니다.')).toBeInTheDocument()
    expect(
      screen.getByText('현재 접근 가능한 문서에서 충분한 근거를 찾지 못했습니다.'),
    ).toBeInTheDocument()
    // A refusal is a normal outcome: no alert role anywhere on the page.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('does not treat a non-refused answer as refused', async () => {
    stubChat({})
    renderChat()

    await screen.findByText('총 사업비는 3억 원입니다.')
    expect(
      screen.queryByText('현재 접근 가능한 문서에서 충분한 근거를 찾지 못했습니다.'),
    ).not.toBeInTheDocument()
  })
})

describe('ChatPage · sources', () => {
  it('shows an accessible source and links to the document detail page', async () => {
    stubChat({})
    renderChat()

    expect(await screen.findByText('출처 1')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: '2026년 사업계획서' })
    expect(link).toHaveAttribute('href', '/documents/doc-1')
    expect(screen.getByText('HWPX')).toBeInTheDocument()
    expect(screen.getByText('문단 32~34')).toBeInTheDocument()
  })

  it.each([
    [{ type: 'paragraph', paragraph_index: 12, paragraph_end: null } as const, '문단 12'],
    [{ type: 'page', page_number: 7 } as const, '7페이지'],
  ])('renders the %s anchor', async (anchor, label) => {
    stubChat({
      detail: () =>
        jsonResponse(
          makeSessionDetail([
            makeAssistantMessage({ sources: [makeAccessibleSource({ anchor })] }),
          ]),
        ),
    })
    renderChat()

    expect(await screen.findByText(label)).toBeInTheDocument()
  })

  it('renders no position at all for a none anchor', async () => {
    stubChat({
      detail: () =>
        jsonResponse(
          makeSessionDetail([
            makeAssistantMessage({
              // HWP/HWPX carry no page number; none is the normal state.
              sources: [makeAccessibleSource({ anchor: { type: 'none' } })],
            }),
          ]),
        ),
    })
    renderChat()

    await screen.findByText('출처 1')
    expect(screen.queryByText(/문단/)).not.toBeInTheDocument()
    expect(screen.queryByText(/페이지/)).not.toBeInTheDocument()
  })

  it('reveals nothing about an inaccessible source', async () => {
    stubChat({
      detail: () =>
        jsonResponse(
          makeSessionDetail([
            makeAssistantMessage({
              content: null,
              has_inaccessible_sources: true,
              content_hidden: true,
              sources: [
                {
                  document_id: 'doc-9',
                  revision_id: 'rev-9',
                  chunk_id: 'chunk-9',
                  accessible: false,
                },
              ],
            }),
          ]),
        ),
    })
    renderChat()

    expect(await screen.findByText('현재 접근할 수 없는 출처')).toBeInTheDocument()
    // No title, no file type, no position, and no link to the document.
    expect(screen.queryByRole('link', { name: /사업계획서/ })).not.toBeInTheDocument()
    expect(screen.queryByText('HWPX')).not.toBeInTheDocument()
    expect(screen.queryByText(/문단/)).not.toBeInTheDocument()
  })

  it('explains a hidden answer instead of rendering a blank bubble', async () => {
    stubChat({
      detail: () =>
        jsonResponse(
          makeSessionDetail([
            makeAssistantMessage({
              content: null,
              refused: false,
              has_inaccessible_sources: true,
              content_hidden: true,
              sources: [
                { document_id: 'd', revision_id: 'r', chunk_id: 'c', accessible: false },
              ],
            }),
          ]),
        ),
    })
    renderChat()

    expect(
      await screen.findByText('이 답변의 근거 문서에 현재 접근할 수 없어 내용을 숨겼습니다.'),
    ).toBeInTheDocument()
  })
})

describe('ChatPage · sending', () => {
  it('posts the message, shows progress, then reloads the stored turn', async () => {
    const { gate, release } = deferred()
    let detailCalls = 0
    const calls = stubChat({
      detail: () => {
        detailCalls += 1
        return jsonResponse(
          detailCalls === 1
            ? makeSessionDetail([])
            : makeSessionDetail([
                makeUserMessage({ content: '예산 알려줘' }),
                makeAssistantMessage(),
              ]),
        )
      },
      send: async () => {
        await gate
        return jsonResponse({
          message_id: 'msg-ai-1',
          answer: '총 사업비는 3억 원입니다.',
          refused: false,
          sources: [makeAccessibleSource()],
          created_at: '2026-09-01T02:00:05Z',
        })
      },
    })
    renderChat()

    const box = await screen.findByLabelText('질문 입력')
    await userEvent.type(box, '예산 알려줘')
    await userEvent.click(screen.getByRole('button', { name: '전송' }))

    expect(await screen.findByText('답변 생성 중...')).toBeInTheDocument()
    release()

    expect(await screen.findByText('총 사업비는 3억 원입니다.')).toBeInTheDocument()
    const send = calls.find((call) => call.url.endsWith('/messages'))
    expect(send?.method).toBe('POST')
    expect(send?.body).toEqual({ message: '예산 알려줘' })
    // The transcript was refetched rather than patched from the response.
    expect(detailCalls).toBeGreaterThan(1)
  })

  it('sends on Enter and inserts a newline on Shift+Enter', async () => {
    const calls = stubChat({
      detail: () => jsonResponse(makeSessionDetail([])),
      send: () =>
        jsonResponse({
          message_id: 'm',
          answer: 'ok',
          refused: false,
          sources: [],
          created_at: '2026-09-01T02:00:05Z',
        }),
    })
    renderChat()

    const box = await screen.findByLabelText('질문 입력')
    await userEvent.type(box, '첫 줄{Shift>}{Enter}{/Shift}둘째 줄')
    expect(box).toHaveValue('첫 줄\n둘째 줄')
    expect(calls.some((call) => call.url.endsWith('/messages'))).toBe(false)

    await userEvent.type(box, '{Enter}')
    await waitFor(() =>
      expect(calls.filter((call) => call.url.endsWith('/messages'))).toHaveLength(1),
    )
  })

  it('does not submit a second time while the first is in flight', async () => {
    const { gate, release } = deferred()
    const calls = stubChat({
      detail: () => jsonResponse(makeSessionDetail([])),
      send: async () => {
        await gate
        return jsonResponse({
          message_id: 'm',
          answer: 'ok',
          refused: false,
          sources: [],
          created_at: '2026-09-01T02:00:05Z',
        })
      },
    })
    renderChat()

    const box = await screen.findByLabelText('질문 입력')
    await userEvent.type(box, '중복 질문')
    const button = screen.getByRole('button', { name: '전송' })
    await userEvent.click(button)

    const sending = await screen.findByRole('button', { name: '전송 중...' })
    expect(sending).toBeDisabled()
    await userEvent.click(sending)
    await userEvent.type(box, '{Enter}')

    release()
    await waitFor(() =>
      expect(calls.filter((call) => call.url.endsWith('/messages'))).toHaveLength(1),
    )
  })

  it('keeps the failed question and offers a retry', async () => {
    stubChat({
      detail: () => jsonResponse(makeSessionDetail([])),
      send: () => errorResponse('INTERNAL_ERROR', '답변 생성을 현재 사용할 수 없습니다.', 500),
    })
    renderChat()

    const box = await screen.findByLabelText('질문 입력')
    await userEvent.type(box, '예산 알려줘')
    await userEvent.click(screen.getByRole('button', { name: '전송' }))

    expect(await screen.findByText('전송 실패')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '다시 시도' })).toBeEnabled()
    // The draft survives so the question is not lost.
    expect(box).toHaveValue('예산 알려줘')
  })
})

describe('ChatPage · errors', () => {
  it('reports a provider failure with the server message and no vendor detail', async () => {
    stubChat({
      detail: () => jsonResponse(makeSessionDetail([])),
      send: () => errorResponse('INTERNAL_ERROR', '답변 생성을 현재 사용할 수 없습니다.', 500),
    })
    renderChat()

    await userEvent.type(await screen.findByLabelText('질문 입력'), '질문')
    await userEvent.click(screen.getByRole('button', { name: '전송' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('답변 생성을 현재 사용할 수 없습니다.')).toBeInTheDocument()
    expect(within(alert).getByText(/문제 신고 번호/)).toBeInTheDocument()
    // Only the server's own message, the request id and the two recovery
    // buttons. Nothing the UI authored about which model or vendor failed --
    // an allow-list, so a future edit cannot introduce vendor wording.
    expect(alert.textContent).toBe(
      '답변 생성을 현재 사용할 수 없습니다.문제 신고 번호: req-test-1다시 시도취소하고 새로고침',
    )
  })

  it('shows the server message for a rejected message (422)', async () => {
    stubChat({
      detail: () => jsonResponse(makeSessionDetail([])),
      send: () =>
        errorResponse('CHAT_MESSAGE_TOO_LONG', '질문은 최대 4000자까지 입력할 수 있습니다.', 422),
    })
    renderChat()

    await userEvent.type(await screen.findByLabelText('질문 입력'), '질문')
    await userEvent.click(screen.getByRole('button', { name: '전송' }))

    const alert = await screen.findByRole('alert')
    expect(
      within(alert).getByText('질문은 최대 4000자까지 입력할 수 있습니다.'),
    ).toBeInTheDocument()
    // Raw validation details are never printed.
    expect(alert.textContent).not.toMatch(/\[|\{|loc|ctx/)
  })

  it('says a session was not found rather than implying a permission problem', async () => {
    stubChat({
      detail: () => errorResponse('CHAT_SESSION_NOT_FOUND', '채팅 세션을 찾을 수 없습니다.', 404),
    })
    renderChat('/chat/ses-someone-else')

    expect(await screen.findByText('대화를 찾을 수 없습니다.')).toBeInTheDocument()
    expect(screen.queryByText(/권한이 없습니다/)).not.toBeInTheDocument()
  })

  it('asks for sign-in on 401 without offering an identity field', async () => {
    stubChat({
      sessions: () => errorResponse('UNAUTHENTICATED', '인증이 필요합니다.', 401),
      detail: () => errorResponse('UNAUTHENTICATED', '인증이 필요합니다.', 401),
    })
    renderChat()

    expect(await screen.findAllByText('로그인이 필요합니다.')).not.toHaveLength(0)
    expect(screen.queryByRole('textbox', { name: /사용자/ })).not.toBeInTheDocument()
  })
})

describe('ChatPage · safety', () => {
  it('renders model and document text as text, never as HTML', async () => {
    const payload = '<img src=x onerror="alert(1)"><b>굵게</b>'
    stubChat({
      detail: () =>
        jsonResponse(
          makeSessionDetail([
            makeUserMessage({ content: payload }),
            makeAssistantMessage({
              content: payload,
              sources: [makeAccessibleSource({ title: payload })],
            }),
          ]),
        ),
    })
    const { container } = renderChat()

    await waitFor(() => expect(screen.getAllByText(payload).length).toBeGreaterThan(0))
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('b')).toBeNull()
  })

  it('talks only to this API and never to an external model service', async () => {
    const calls = stubChat({})
    renderChat()
    await screen.findByText('총 사업비는 3억 원입니다.')

    expect(calls.length).toBeGreaterThan(0)
    for (const call of calls) {
      // An allow-list rather than a vendor deny-list: every request is
      // same-origin and under this API's prefix, which leaves no room for a
      // model service of any name.
      expect(call.url.startsWith('/api/v1/')).toBe(true)
      expect(call.url).not.toMatch(/^[a-z]+:\/\//i)
    }
  })
})
