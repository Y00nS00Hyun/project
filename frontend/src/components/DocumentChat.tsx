import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiClientError } from '../api/client'
import { createDocumentSession, fetchSession, fetchSessions, sendMessage } from '../api/chat'
import { ChatComposer } from './ChatComposer'
import { ChatMessages, type PendingTurn } from './ChatMessages'
import { ErrorView, LoadingState } from './StateViews'
import { useAsyncResource } from '../hooks/useAsyncResource'

/**
 * Ask questions about one document, on that document's page.
 *
 * The scope is not enforced here. It is stored on the session and applied by
 * the server in the same SQL stage as the ACL, so this component cannot widen
 * it and a bug in it cannot leak another document. What it does is far
 * smaller: pick the right session, and say plainly which document the answers
 * come from.
 *
 * The session is created on the first question rather than on mount. Creating
 * one per page view would fill the chat list with empty sessions belonging to
 * documents nobody actually asked about.
 */
export function DocumentChat({
  documentId,
  title,
  available,
}: {
  documentId: string
  title: string
  /**
   * From the document detail response. When false, no provider may be called
   * for this corpus, and asking would fail after the question was typed -- so
   * the box is disabled up front instead.
   */
  available: boolean
}) {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const [sending, setSending] = useState(false)
  const [pending, setPending] = useState<PendingTurn | null>(null)
  const [error, setError] = useState<ApiClientError | null>(null)

  // Reuse the most recent session already bound to this document, so returning
  // to the page continues the conversation instead of starting a new one. Only
  // the first page of sessions is searched: finding nothing here creates a new
  // session, which is correct, just not a continuation of a very old one.
  const existing = useAsyncResource(
    async (signal) => {
      const page = await fetchSessions(1, 20, { signal })
      const match = page.items.find(
        (item) => item.document_scope?.document_id === documentId,
      )
      return match?.session_id ?? null
    },
    [documentId],
  )

  const active = sessionId ?? existing.data ?? null

  const detail = useAsyncResource(
    (signal) => fetchSession(active as string, 1, 50, { signal }),
    [active, version],
    Boolean(active),
  )

  const onSend = useCallback(
    async (message: string): Promise<boolean> => {
      // The composer is disabled too. Repeated here because the capability can
      // change under a page that has been open for a while, and because a
      // disabled control is a UI convenience, not a guarantee.
      if (sending || !available) return false
      setSending(true)
      setError(null)
      setPending({ text: message, failed: false })
      try {
        // Created lazily, and bound to this document by the server at creation.
        let target = active
        if (!target) {
          const session = await createDocumentSession(documentId, title)
          target = session.session_id
          setSessionId(target)
        }
        await sendMessage(target, message)
        setPending(null)
        // Refetch rather than append: what the screen shows is then exactly
        // what was stored, including the sources the server actually attached.
        setVersion((value) => value + 1)
        return true
      } catch (caught) {
        // The turn may or may not have been stored. Keep the text on screen,
        // marked as failed, rather than claiming either outcome.
        setPending({ text: message, failed: true })
        setError(asApiError(caught))
        return false
      } finally {
        setSending(false)
      }
    },
    [active, available, documentId, title, sending],
  )

  const messages = detail.data?.messages.items ?? []

  return (
    <section className="section" aria-labelledby="document-chat-heading">
      <h2 className="section-title" id="document-chat-heading">
        이 문서에 질문하기
      </h2>
      {available ? (
        <p className="state-hint">
          답변은 이 문서의 현재 검색 버전 내용만을 근거로 합니다. 다른 문서는 참고하지 않습니다.
        </p>
      ) : (
        <p className="notice">AI 질문 기능이 현재 비활성화되어 있습니다.</p>
      )}

      {existing.error && <ErrorView error={existing.error} />}
      {detail.error && !detail.error.isNotFound && <ErrorView error={detail.error} />}

      {active && !detail.data && !detail.error ? (
        <LoadingState label="대화를 불러오는 중..." />
      ) : (
        <ChatMessages messages={messages} pending={pending} />
      )}

      {messages.length === 0 && !pending && (
        <p className="state-hint">아직 주고받은 질문이 없습니다.</p>
      )}

      <ChatComposer
        onSend={onSend}
        sending={sending}
        disabled={!available}
        placeholder={
          available ? undefined : 'AI 질문 기능이 현재 비활성화되어 있습니다.'
        }
      />
      {error && <ErrorView error={error} />}

      {active && (
        <p className="state-hint">
          <Link to={`/chat/${active}`}>전체 화면에서 이어서 대화하기</Link>
        </p>
      )}
    </section>
  )
}

function asApiError(error: unknown): ApiClientError {
  return error instanceof ApiClientError
    ? error
    : new ApiClientError('INTERNAL_ERROR', '답변을 가져오지 못했습니다.', 0, null)
}
