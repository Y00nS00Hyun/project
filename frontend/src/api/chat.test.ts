import { createSession, fetchSession, fetchSessions, sendMessage } from './chat'
import { ApiClientError } from './client'
import { errorResponse, jsonResponse } from '../test/helpers'

function init(): RequestInit {
  return vi.mocked(fetch).mock.calls[0][1] as RequestInit
}

function url(): string {
  return String(vi.mocked(fetch).mock.calls[0][0])
}

describe('chat api', () => {
  beforeEach(() => vi.stubGlobal('fetch', vi.fn()))

  it('lists sessions with pagination and no identity parameter', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ items: [], page: 1, size: 20, total: 0 }))
    await fetchSessions()

    expect(url()).toBe('/api/v1/chat/sessions?page=1&size=20')
    expect(init().method).toBe('GET')
    // A GET carries no body and therefore no Content-Type.
    expect(Object.keys(init().headers as Record<string, string>)).not.toContain('Content-Type')
  })

  it('creates a session with the contract minimum body', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ session_id: 's' }))
    await createSession()

    expect(url()).toBe('/api/v1/chat/sessions')
    expect(init().method).toBe('POST')
    expect(init().body).toBe('{}')
    expect((init().headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('includes a title only when one was given', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ session_id: 's' }))
    await createSession('예산 문의')

    expect(init().body).toBe(JSON.stringify({ title: '예산 문의' }))
  })

  it('escapes the session id in the detail path', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ session_id: 's' }))
    await fetchSession('a/../b')

    expect(url()).toBe('/api/v1/chat/sessions/a%2F..%2Fb?page=1&size=50')
  })

  it('sends exactly the message field the schema declares', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ message_id: 'm' }))
    await sendMessage('ses-1', '사업 예산이 얼마야?')

    expect(url()).toBe('/api/v1/chat/sessions/ses-1/messages')
    expect(JSON.parse(String(init().body))).toEqual({ message: '사업 예산이 얼마야?' })
  })

  it('reports chat failures through the shared error envelope', async () => {
    vi.mocked(fetch).mockResolvedValue(
      errorResponse('CHAT_SESSION_NOT_FOUND', '채팅 세션을 찾을 수 없습니다.', 404),
    )

    await expect(fetchSession('ses-x')).rejects.toMatchObject({
      code: 'CHAT_SESSION_NOT_FOUND',
      message: '채팅 세션을 찾을 수 없습니다.',
      status: 404,
      requestId: 'req-test-1',
    })
    await expect(fetchSession('ses-x')).rejects.toBeInstanceOf(ApiClientError)
  })

  it('turns a rate-limited provider into the same envelope error', async () => {
    vi.mocked(fetch).mockResolvedValue(
      errorResponse('RATE_LIMITED', '요청이 많아 잠시 후 다시 시도해 주세요.', 429),
    )

    await expect(sendMessage('ses-1', '질문')).rejects.toMatchObject({
      code: 'RATE_LIMITED',
      status: 429,
    })
  })
})
