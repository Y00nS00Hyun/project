import { getJson, postJson, type RequestOptions } from './client'
import {
  CHAT_MESSAGE_PAGE_SIZE,
  CHAT_SESSION_PAGE_SIZE,
  type ChatSession,
  type ChatSessionDetail,
  type ChatSessionListResponse,
  type CreateSessionRequest,
  type SendMessageRequest,
  type SendMessageResponse,
} from './types'

/**
 * The caller's own sessions.
 *
 * No user filter is sent and none is applied here: the backend scopes the list
 * to the authenticated user. Filtering client-side would imply the response
 * could contain somebody else's session, which it cannot.
 */
export function fetchSessions(
  page = 1,
  size = CHAT_SESSION_PAGE_SIZE,
  options: RequestOptions = {},
): Promise<ChatSessionListResponse> {
  return getJson<ChatSessionListResponse>('/chat/sessions', {
    ...options,
    params: { page, size },
  })
}

/**
 * Start a session.
 *
 * `title` is optional in the contract, so the default request body is `{}` --
 * the minimum the schema allows. Identity is never part of the body: the
 * request shape forbids extra fields, and the server takes the user from
 * authentication.
 */
export function createSession(
  title: string | null = null,
  options: RequestOptions = {},
): Promise<ChatSession> {
  const body: CreateSessionRequest = title ? { title } : {}
  return postJson<ChatSession>('/chat/sessions', body, options)
}

/**
 * Start a session that can only draw on one document.
 *
 * The document is named here and nowhere else. Every later message goes
 * through the ordinary `sendMessage`, because the scope lives on the session
 * server-side -- there is nothing for the client to keep resending, and so
 * nothing it can get wrong.
 *
 * A caller without read permission on the document gets the same 404 as for a
 * document that does not exist.
 */
export function createDocumentSession(
  documentId: string,
  title: string | null = null,
  options: RequestOptions = {},
): Promise<ChatSession> {
  const body: CreateSessionRequest = { document_id: documentId }
  if (title) body.title = title
  return postJson<ChatSession>('/chat/sessions', body, options)
}

export function fetchSession(
  sessionId: string,
  page = 1,
  size = CHAT_MESSAGE_PAGE_SIZE,
  options: RequestOptions = {},
): Promise<ChatSessionDetail> {
  return getJson<ChatSessionDetail>(`/chat/sessions/${encodeURIComponent(sessionId)}`, {
    ...options,
    params: { page, size },
  })
}

/**
 * Ask a question in an existing session.
 *
 * Deliberately not abortable by default. The server persists the user message,
 * the answer and its sources in one transaction, so abandoning the request
 * client-side would not undo the turn -- it would only hide it until the next
 * refresh. The page refetches instead.
 */
export function sendMessage(
  sessionId: string,
  message: string,
  options: RequestOptions = {},
): Promise<SendMessageResponse> {
  const body: SendMessageRequest = { message }
  return postJson<SendMessageResponse>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
    body,
    options,
  )
}
