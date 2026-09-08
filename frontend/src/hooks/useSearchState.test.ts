import { describe, expect, it } from 'vitest'
import { parseSearchState, toSearchParams } from './useSearchState'

describe('parseSearchState', () => {
  it('reads every filter out of the URL', () => {
    const state = parseSearchState(
      new URLSearchParams('q=사업계획&page=2&department_id=dep-1&year=2026&tag_id=12&tag_id=13&file_type=hwpx'),
    )
    expect(state).toEqual({
      q: '사업계획',
      page: 2,
      departmentId: 'dep-1',
      year: 2026,
      tagIds: [12, 13],
      fileType: 'hwpx',
    })
  })

  it('defaults to a browse state when the URL is empty', () => {
    expect(parseSearchState(new URLSearchParams())).toEqual({
      q: '',
      page: 1,
      departmentId: null,
      year: null,
      tagIds: [],
      fileType: null,
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
      departmentId: 'dep-1',
      year: 2026,
      tagIds: [12, 13],
      fileType: 'hwpx' as const,
    }
    expect(parseSearchState(toSearchParams(state))).toEqual(state)
  })

  it('leaves defaults out of the URL', () => {
    const params = toSearchParams({
      q: '',
      page: 1,
      departmentId: null,
      year: null,
      tagIds: [],
      fileType: null,
    })
    expect(params.toString()).toBe('')
  })

  it('repeats tag_id so the backend ANDs the tags', () => {
    const params = toSearchParams({
      q: '',
      page: 1,
      departmentId: null,
      year: null,
      tagIds: [12, 13],
      fileType: null,
    })
    expect(params.getAll('tag_id')).toEqual(['12', '13'])
  })
})
