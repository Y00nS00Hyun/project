import { useId, useState, type FormEvent, type KeyboardEvent } from 'react'
import { MESSAGE_MAX_LENGTH } from '../api/types'

/**
 * The question box.
 *
 * The length cap mirrors the served schema exactly rather than being a stricter
 * client policy, so nothing that the API would accept is blocked here.
 */
export function ChatComposer({
  onSend,
  sending,
  disabled = false,
  placeholder,
}: {
  /** Resolves true when the turn was stored, so the draft can be cleared. */
  onSend: (message: string) => Promise<boolean>
  sending: boolean
  /**
   * The feature itself is unavailable, as opposed to a turn being in flight.
   * The field is disabled rather than hidden so the reader can see what is
   * missing instead of wondering where it went.
   */
  disabled?: boolean
  placeholder?: string
}) {
  const [value, setValue] = useState('')
  const fieldId = useId()
  const empty = value.trim().length === 0
  const blocked = sending || disabled

  async function submit() {
    // Guarded here as well as by the disabled button: a double Enter can fire
    // twice before React has re-rendered the disabled state.
    if (blocked || empty) return
    if (await onSend(value.trim())) setValue('')
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter starts a new line. IME composition must be left
    // alone or Korean input commits a half-typed syllable as a question.
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
    event.preventDefault()
    void submit()
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    void submit()
  }

  return (
    <form className="chat-composer" onSubmit={onSubmit}>
      <label className="visually-hidden" htmlFor={fieldId}>
        질문 입력
      </label>
      <textarea
        id={fieldId}
        className="chat-input"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={onKeyDown}
        maxLength={MESSAGE_MAX_LENGTH}
        rows={3}
        placeholder={
          placeholder ?? '문서에 대해 궁금한 내용을 질문해 보세요. (Enter 전송, Shift+Enter 줄바꿈)'
        }
        disabled={blocked}
      />
      <div className="chat-composer-footer">
        <span className="chat-counter" aria-hidden="true">
          {value.length} / {MESSAGE_MAX_LENGTH}
        </span>
        <button className="button button-primary" type="submit" disabled={blocked || empty}>
          {sending ? '전송 중...' : '전송'}
        </button>
      </div>
    </form>
  )
}
