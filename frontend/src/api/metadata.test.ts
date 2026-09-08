import { afterEach, expect, it, vi } from 'vitest'
import { fetchTags } from './metadata'
import { errorResponse, jsonResponse } from '../test/helpers'

afterEach(() => vi.unstubAllGlobals())

it('loads every tag page, including tags beyond the first 100', async () => {
  const signal = new AbortController().signal
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse({ items: [{ id: 1, name: '첫 태그' }], page: 1, size: 100, total: 101 }))
    .mockResolvedValueOnce(jsonResponse({ items: [{ id: 101, name: '마지막 태그' }], page: 2, size: 100, total: 101 }))
  vi.stubGlobal('fetch', fetchMock)
  expect((await fetchTags({ signal })).items.map((tag) => tag.id)).toEqual([1, 101])
  expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/tags?page=2&size=100')
  expect(fetchMock.mock.calls.every((call) => call[1].signal === signal)).toBe(true)
})

it('reports a later-page error instead of silently offering a partial vocabulary', async () => {
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(jsonResponse({ items: [{ id: 1, name: '태그' }], page: 1, size: 100, total: 101 }))
    .mockResolvedValueOnce(errorResponse('INTERNAL_ERROR', '태그 조회 실패', 500)))
  await expect(fetchTags()).rejects.toThrow('태그 조회 실패')
})
