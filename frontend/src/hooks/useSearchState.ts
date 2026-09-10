import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { FILE_TYPES, type FileType } from '../api/types'

/**
 * Search state lives in the URL, not in component state.
 *
 * That makes the current search reloadable, shareable and back-button correct
 * for free -- the browser history is the state history.
 */
export interface SearchState {
  q: string
  page: number
  year: number | null
  tagIds: number[]
  fileType: FileType | null
}

function parseYear(raw: string | null): number | null {
  if (!raw) return null
  const value = Number(raw)
  // Mirrors the backend bound. An out-of-range value in a pasted URL is
  // dropped rather than sent, so the page does not open on a 422.
  return Number.isInteger(value) && value >= 1900 && value <= 2100 ? value : null
}

function parseFileType(raw: string | null): FileType | null {
  return raw && (FILE_TYPES as readonly string[]).includes(raw) ? (raw as FileType) : null
}

export function parseSearchState(params: URLSearchParams): SearchState {
  const page = Number(params.get('page'))
  return {
    q: params.get('q') ?? '',
    page: Number.isInteger(page) && page >= 1 ? page : 1,
    year: parseYear(params.get('year')),
    tagIds: params
      .getAll('tag_id')
      .map(Number)
      .filter((id) => Number.isSafeInteger(id) && id > 0),
    fileType: parseFileType(params.get('file_type')),
  }
}

export function toSearchParams(state: SearchState): URLSearchParams {
  const params = new URLSearchParams()
  if (state.q.trim()) params.set('q', state.q.trim())
  if (state.year != null) params.set('year', String(state.year))
  // Repeated key: the backend ANDs multiple tag_id values.
  for (const id of state.tagIds) params.append('tag_id', String(id))
  if (state.fileType) params.set('file_type', state.fileType)
  if (state.page > 1) params.set('page', String(state.page))
  return params
}

export function useSearchState() {
  const [params, setParams] = useSearchParams()
  const state = useMemo(() => parseSearchState(params), [params])

  const update = useCallback(
    (patch: Partial<SearchState>) => {
      // Any change other than paging returns to page 1: staying on page 4 of a
      // different result set shows an empty page for no visible reason.
      const next: SearchState = {
        ...state,
        ...patch,
        page: patch.page ?? (Object.keys(patch).length > 0 ? 1 : state.page),
      }
      setParams(toSearchParams(next))
    },
    [state, setParams],
  )

  const goToPage = useCallback((page: number) => update({ page }), [update])

  return { state, update, goToPage }
}
