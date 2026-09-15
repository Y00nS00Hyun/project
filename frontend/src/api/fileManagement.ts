import { getJson, postJson, type RequestOptions } from './client'
import type { AdminDirectory, AdminDirectoryListResponse, CreateDirectoryRequest } from './types'

/**
 * Real directories under the shared folder, empty ones included.
 *
 * Administrators only, and only while file management is on. Used to choose
 * where to create a folder or move a document -- never for the sidebar, which
 * stays GET /folders.
 */
export function fetchAdminDirectories(
  options: RequestOptions = {},
): Promise<AdminDirectoryListResponse> {
  return getJson<AdminDirectoryListResponse>('/admin/directories', options)
}

export function createAdminDirectory(
  body: CreateDirectoryRequest,
  options: RequestOptions = {},
): Promise<AdminDirectory> {
  return postJson<AdminDirectory>('/admin/directories', body, options)
}
