import { describe, expect, it } from 'vitest'
import { parseSearchState, toSearchParams } from './useSearchState'

describe('parseSearchState', () => {
  it('reads every filter out of the URL', () => {
    // department_id is present on purpose: it is no longer a user-facing
    // filter, so a stale link carrying one must be ignored rather than
    // silently narrowing the results to a department the user cannot see.
    const state = parseSearchState(
      new URLSearchParams('q=사업계획&page=2&department_id=dep-1&year=2026&tag_id=12&tag_id=13&file_type=hwpx'),
    )
    expect(state).toEqual({
      q: '사업계획',
      page: 2,
      year: 2026,
      tagIds: [12, 13],
      fileType: 'hwpx',
      folderPath: null,
      topLevelOnly: false,
    })
  })

  it('defaults to a browse state when the URL is empty', () => {
    expect(parseSearchState(new URLSearchParams())).toEqual({
      q: '',
      page: 1,
      year: null,
      tagIds: [],
      fileType: null,
      folderPath: null,
      topLevelOnly: false,
    })
  })

  it('drops a year outside the backend bound rather than sending a 422', () => {
    expect(parseSearchState(new URLSearchParams('year=1800')).year).toBeNull()
    expect(parseSearchState(new URLSearchParams('year=abc')).year).toBeNull()
  })

  it('drops a file_type the backend does not accept', () => {
    // The enum is the backend's; the client never widens it.
    expect(parseSearchState(new URLSearchParams('file_type=exe')).fileType).toBeNull()
    expect(parseSearchState(new URLSearchParams('file_type=pdf')).fileType).toBe('pdf')
  })

  it('falls back to page 1 for a nonsense page', () => {
    expect(parseSearchState(new URLSearchParams('page=0')).page).toBe(1)
    expect(parseSearchState(new URLSearchParams('page=-3')).page).toBe(1)
  })
})

describe('toSearchParams', () => {
  it('round-trips a full state so a shared link restores it', () => {
    const state = {
      q: '사업계획',
      page: 3,
      year: 2026,
      tagIds: [12, 13],
      fileType: 'hwpx' as const,
      folderPath: null,
      topLevelOnly: false,
    }
    expect(parseSearchState(toSearchParams(state))).toEqual(state)
  })

  it('leaves defaults out of the URL', () => {
    const params = toSearchParams({
      q: '',
      page: 1,
      year: null,
      tagIds: [],
      fileType: null,
      folderPath: null,
      topLevelOnly: false,
    })
    expect(params.toString()).toBe('')
  })

  it('does not put department_id back into the URL', () => {
    // The department control was removed from the UI; the backend still
    // supports the parameter, but nothing in the UI should emit it.
    const params = toSearchParams({
      q: '사업계획',
      page: 1,
      year: 2026,
      tagIds: [12],
      fileType: 'hwpx',
      folderPath: null,
      topLevelOnly: false,
    })
    expect(params.has('department_id')).toBe(false)
  })

  it('repeats tag_id so the backend ANDs the tags', () => {
    const params = toSearchParams({
      q: '',
      page: 1,
      year: null,
      tagIds: [12, 13],
      fileType: null,
      folderPath: null,
      topLevelOnly: false,
    })
    expect(params.getAll('tag_id')).toEqual(['12', '13'])
  })
})

describe('top-level-only is exclusive with folderPath', () => {
  it('round-trips through the URL', () => {
    const state = {
      q: '', page: 1, year: null, tagIds: [], fileType: null,
      folderPath: null, topLevelOnly: true,
    }
    expect(toSearchParams(state).get('top_level_only')).toBe('1')
    expect(parseSearchState(toSearchParams(state))).toEqual(state)
  })

  it('is absent from the URL when off', () => {
    const params = toSearchParams({
      q: '', page: 1, year: null, tagIds: [], fileType: null,
      folderPath: null, topLevelOnly: false,
    })
    // The backend's default is false; sending it would put a meaningless
    // parameter in every shared link.
    expect(params.has('top_level_only')).toBe(false)
  })

  it('a folder path wins over a hand-edited top_level_only', () => {
    // "inside this folder" and "inside no folder" cannot both hold, and the
    // backend refuses the combination. Preferring the specific one keeps a
    // pasted URL working instead of 422-ing.
    const state = parseSearchState(
      new URLSearchParams('folder_path=HELLO&top_level_only=1'),
    )
    expect(state.folderPath).toBe('HELLO')
    expect(state.topLevelOnly).toBe(false)
  })

  it('serialising never emits both', () => {
    const params = toSearchParams({
      q: '', page: 1, year: null, tagIds: [], fileType: null,
      folderPath: 'HELLO', topLevelOnly: true,
    })
    expect(params.get('folder_path')).toBe('HELLO')
    expect(params.has('top_level_only')).toBe(false)
  })
})
