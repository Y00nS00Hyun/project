import { useCallback, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ApiClientError } from '../api/client'
import { createSession, fetchSession, fetchSessions, sendMessage } from '../api/chat'
import { AppNav } from '../components/AppNav'
import { ChatComposer } from '../components/ChatComposer'
import { ChatMessages, type PendingTurn } from '../components/ChatMessages'
import { SessionList } from '../components/SessionList'
import { ErrorView, LoadingState } from '../components/StateViews'
import { useAsyncResource } from '../hooks/useAsyncResource'

export function ChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()

  // Bumped to refetch after a write, so the transcript and the sidebar counts
  // come from the server rather than being patched locally.
  const [version, setVersion] = useState(0)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<ApiClientError | null>(null)
  const [sending, setSending] = useState(false)
  const [pending, setPending] = useState<PendingTurn | null>(null)
  const [sendError, setSendError] = useState<ApiClientError | null>(null)

  const sessions = useAsyncResource((signal) => fetchSessions(1, 20, { signal }), [version])
  const detail = useAsyncResource(
    (signal) => fetchSession(sessionId as string, 1, 50, { signal }),
    [sessionId, version],
    Boolean(sessionId),
  )

  const onCreate = useCallback(async () => {
    if (creating) return
    setCreating(true)
    setCreateError(null)
    try {
      // No title, no identity: the contract's minimum body is `{}`.
      const session = await createSession()
      setVersion((value) => value + 1)
      navigate(`/chat/${session.session_id}`)
    } catch (error) {
      setCreateError(asApiError(error))
    } finally {
      setCreating(false)
    }
  }, [creating, navigate])

  const onSend = useCallback(
    async (message: string): Promise<boolean> => {
      // Second guard against a double submit; the button is disabled too.
      if (!sessionId || sending) return false
      setSending(true)
      setSendError(null)
      setPending({ text: message, failed: false })
      try {
        await sendMessage(sessionId, message)
        // The response carries the answer, but not the stored user message.
        // Refetching is the honest option: what the screen shows is then
        // exactly what the database holds, including the turn's real ids.
        setPending(null)
        setVersion((value) => value + 1)
        return true
      } catch (error) {
        // The turn may or may not have been stored. Keep the text visible and
        // marked as failed rather than claiming either outcome.
        setPending({ text: message, failed: true })
        setSendError(asApiError(error))
        return false
      } finally {
        setSending(false)
      }
    },
    [sessionId, sending],
  )

  const onRetry = useCallback(() => {
    if (!pending) return
    void onSend(pending.text)
  }, [pending, onSend])

  const onDismiss = useCallback(() => {
    setPending(null)
    setSendError(null)
    setVersion((value) => value + 1)
  }, [])

  return (
    <main className="page page-wide">
      <AppNav />
      <h1 className="page-title">AI 문서 질문</h1>

      <div className="chat-layout">
        <aside className="chat-sidebar">
          <SessionList
            sessions={sessions.data?.items ?? []}
            loading={sessions.loading}
            creating={creating}
            onCreate={onCreate}
          />
          {sessions.error && <ErrorView error={sessions.error} />}
          {createError && <ErrorView error={createError} />}
        </aside>

        <section className="chat-main">
          {!sessionId ? (
            <p className="state" role="status">
              왼쪽에서 대화를 선택하거나 새 대화를 시작해 주세요.
            </p>
          ) : detail.error ? (
            <SessionError error={detail.error} />
          ) : detail.loading && !detail.data ? (
            <LoadingState label="대화를 불러오는 중..." />
          ) : detail.data ? (
            <>
              <h2 className="chat-session-title">{detail.data.title ?? '제목 없는 대화'}</h2>

              {detail.data.messages.items.length === 0 && !pending ? (
                <p className="state" role="status">
                  문서에 대해 궁금한 내용을 질문해 보세요.
                </p>
              ) : (
                <ChatMessages messages={detail.data.messages.items} pending={pending} />
              )}

              {/* No streaming exists on the server, so there is no typing
                  animation here pretending otherwise -- the whole answer
                  appears when the response lands. */}
              <div aria-live="polite" aria-atomic="true">
                {sending && <LoadingState label="답변 생성 중..." />}
              </div>

              {sendError && (
                <div className="state state-error" role="alert">
                  <p className="state-message">{sendError.message}</p>
                  {sendError.requestId && (
                    <p className="request-id">문제 신고 번호: {sendError.requestId}</p>
                  )}
                  <p className="chat-retry-row">
                    <button className="button button-quiet" type="button" onClick={onRetry} disabled={sending}>
                      다시 시도
                    </button>
                    <button className="button button-quiet" type="button" onClick={onDismiss} disabled={sending}>
                      취소하고 새로고침
                    </button>
                  </p>
                </div>
              )}

              <ChatComposer onSend={onSend} sending={sending} />
            </>
          ) : null}
        </section>
      </div>
    </main>
  )
}

/**
 * A missing session and somebody else's session are the same 404 by design, so
 * the wording says "not found" and never implies a permission problem -- doing
 * otherwise would confirm that an id exists.
 */
function SessionError({ error }: { error: ApiClientError }) {
  if (error.isNotFound) {
    return (
      <div className="state state-error" role="alert">
        <p className="state-message">대화를 찾을 수 없습니다.</p>
      </div>
    )
  }
  return <ErrorView error={error} />
}

function asApiError(error: unknown): ApiClientError {
  return error instanceof ApiClientError
    ? error
    : new ApiClientError('INTERNAL_ERROR', '알 수 없는 오류가 발생했습니다.', 0, null)
}
