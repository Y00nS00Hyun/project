import { getFile, getJson, type DownloadedFile, type RequestOptions } from './client'
import type {
  DocumentDetail,
  RevisionListResponse,
  TextPreviewResponse,
} from './types'

export function fetchDocument(
  documentId: string,
  options: RequestOptions = {},
): Promise<DocumentDetail> {
  return getJson<DocumentDetail>(`/documents/${encodeURIComponent(documentId)}`, options)
}

export function fetchRevisions(
  documentId: string,
  page = 1,
  size = 20,
  options: RequestOptions = {},
): Promise<RevisionListResponse> {
  return getJson<RevisionListResponse>(
    `/documents/${encodeURIComponent(documentId)}/revisions`,
    { ...options, params: { page, size } },
  )
}

/**
 * Fetch the original file.
 *
 * Deliberately not a plain <a href> navigation: going through fetch keeps the
 * request on the same code path as every other call, so a 404 or 409 arrives
 * as the normal error envelope and can be shown in the page instead of
 * replacing it with a JSON error document. It also carries the development
 * identity header, which a link navigation cannot.
 *
 * Only document_id is sent. The client never knows or constructs a filesystem
 * path -- the backend authorises first, then resolves the path itself.
 */
export function downloadDocument(
  documentId: string,
  fallbackName: string,
  options: RequestOptions = {},
): Promise<DownloadedFile> {
  return getFile(
    `/documents/${encodeURIComponent(documentId)}/download`,
    fallbackName,
    options,
  )
}

/**
 * The text the parser extracted from the document's current READY revision.
 *
 * Its own request rather than a field on the detail response: a long report is
 * hundreds of thousands of characters, and most people opening a document page
 * never ask to read it there.
 *
 * Paged by block -- a block is a chunk, the same unit search matches and
 * citations point at, so what is shown here and what an answer quotes describe
 * the same place.
 */
export function fetchDocumentText(
  documentId: string,
  offset = 0,
  limit = 20,
  options: RequestOptions = {},
): Promise<TextPreviewResponse> {
  return getJson<TextPreviewResponse>(
    `/documents/${encodeURIComponent(documentId)}/text`,
    { ...options, params: { offset, limit } },
  )
}
