import { getJson, type RequestOptions } from './client'
import type { DepartmentListResponse, TagListResponse } from './types'

/** Filter vocabulary comes from the server. Nothing here is hard-coded. */
export function fetchDepartments(options: RequestOptions = {}): Promise<DepartmentListResponse> {
  return getJson<DepartmentListResponse>('/departments', options)
}

export async function fetchTags(options: RequestOptions = {}): Promise<Pick<TagListResponse, 'items'>> {
  const items: TagListResponse['items'] = []
  let page = 1
  while (true) {
    const result = await getJson<TagListResponse>('/tags', {
      ...options, params: { page, size: 100 },
    })
    items.push(...result.items)
    if (result.items.length === 0 || result.page * result.size >= result.total) break
    page = result.page + 1
  }
  return { items }
}
