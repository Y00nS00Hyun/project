import { useEffect, useState } from 'react'

interface Props {
  value: string
  onSubmit: (q: string) => void
}

/**
 * Submit-driven, never keystroke-driven.
 *
 * A semantic search embeds the query and scans vectors; firing that per
 * keystroke would be one model call per character. The input holds a local
 * draft and only the committed value reaches the URL and the API.
 */
export function SearchForm({ value, onSubmit }: Props) {
  const [draft, setDraft] = useState(value)

  // Keep the box in step with the URL when the user navigates back or opens a
  // shared link.
  useEffect(() => setDraft(value), [value])

  return (
    <form
      className="search-form"
      role="search"
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit(draft)
      }}
    >
      <label className="visually-hidden" htmlFor="search-input">
        검색어
      </label>
      <input
        id="search-input"
        className="search-input"
        type="search"
        name="q"
        value={draft}
        placeholder="문서명이나 내용을 입력하세요"
        autoComplete="off"
        onChange={(event) => setDraft(event.target.value)}
      />
      <button className="button button-primary" type="submit">
        검색
      </button>
    </form>
  )
}
