import { getJson, type RequestOptions } from './client'
import type { FileType, SearchResponse } from './types'

/**
 * Everything the search screen can vary.
 *
 * `mode` is intentionally absent. Contract section 6.3 recommends not exposing
 * the retrieval route as an algorithm picker, and omitting the parameter lets
 * the server keep choosing (today semantic, later possibly RRF) without any
 * client change.
 */
export interface SearchQuery {
  q?: string
  page?: number
  size?: number
  departmentId?: string | null
  /** Only documents in no folder. Never sent together with folderPath. */
  topLevelOnly?: boolean
  year?: number | null
  tagIds?: number[]
  fileType?: FileType | null
  /** Canonical folder path; restricts results to its whole subtree. */
  folderPath?: string | null
}

export function searchDocuments(
  query: SearchQuery,
  options: RequestOptions = {},
): Promise<SearchResponse> {
  const q = query.q?.trim()
  return getJson<SearchResponse>('/search', {
    ...options,
    params: {
      // Omitted entirely when blank: the backend then runs browse mode
      // (updated_at DESC) rather than a retrieval with an empty query.
      q: q || undefined,
      page: query.page,
      size: query.size,
      department_id: query.departmentId,
      // Omitted entirely when false: the backend's default is false, and
      // sending it would put a meaningless parameter in every shared URL.
      top_level_only: query.topLevelOnly ? true : undefined,
      year: query.year,
      tag_id: query.tagIds,
      file_type: query.fileType,
      folder_path: query.folderPath,
    },
  })
}
