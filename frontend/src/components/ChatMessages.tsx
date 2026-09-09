import type { AssistantChatMessage, ChatMessage } from '../api/types'
import { ChatSources } from './ChatSources'

/** A question that has been sent but has no server answer yet. */
export interface PendingTurn {
  text: string
  failed: boolean
}

/**
 * The transcript, in the order the server returned it.
 *
 * No client-side re-sorting: ordering is the server's (created_at, id), and
 * re-deriving it here from timestamps would eventually disagree with the
 * pagination the same endpoint applies.
 */
export function ChatMessages({
  messages,
  pending,
}: {
  messages: ChatMessage[]
  pending: PendingTurn | null
}) {
  return (
    <ol className="chat-log">
      {messages.map((message) => (
        <li className="chat-turn" key={message.message_id}>
          {message.role === 'assistant' ? (
            <AssistantTurn message={message} />
          ) : (
            <PlainTurn message={message} />
          )}
        </li>
      ))}
      {pending && (
        <li className="chat-turn" key="pending">
          <article className="chat-bubble chat-user is-pending">
            <RoleLabel role="user" />
            <p className="chat-text">{pending.text}</p>
            <p className="chat-pending-note">
              {pending.failed ? '전송 실패' : '전송 중...'}
            </p>
          </article>
        </li>
      )}
    </ol>
  )
}

const ROLE_LABELS = { user: '사용자', assistant: 'AI', system: '시스템' } as const

function RoleLabel({ role }: { role: keyof typeof ROLE_LABELS }) {
  return <p className="chat-role">{ROLE_LABELS[role]}</p>
}

function PlainTurn({ message }: { message: Exclude<ChatMessage, AssistantChatMessage> }) {
  return (
    <article className={`chat-bubble chat-${message.role}`}>
      <RoleLabel role={message.role} />
      {/* Plain text through React's own escaping. Never dangerouslySetInnerHTML:
          this string came from a user, and the answer below it from a model
          reading documents. */}
      <p className="chat-text">{message.content}</p>
    </article>
  )
}

function AssistantTurn({ message }: { message: AssistantChatMessage }) {
  return (
    <article className={`chat-bubble chat-assistant${message.refused ? ' is-refused' : ''}`}>
      <RoleLabel role="assistant" />

      {message.content_hidden ? (
        // Not a rendering bug and not an error: the answer is stored, but a
        // document behind it is no longer readable, so the server withheld the
        // text. `content_hidden` says so explicitly -- a null `content` alone
        // would not.
        <p className="chat-notice" role="note">
          이 답변의 근거 문서에 현재 접근할 수 없어 내용을 숨겼습니다.
        </p>
      ) : (
        <p className="chat-text">{message.content}</p>
      )}

      {/* refused is the server's own boolean. A refusal is a normal outcome,
          so it reads as a note rather than an error. */}
      {message.refused && !message.content_hidden && (
        <p className="chat-refusal-note">
          현재 접근 가능한 문서에서 충분한 근거를 찾지 못했습니다.
        </p>
      )}

      <ChatSources sources={message.sources} />
    </article>
  )
}
