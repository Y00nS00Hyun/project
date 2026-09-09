/**
 * Response types mirroring the FastAPI OpenAPI document
 * (docs/api-contract-v1.md, src/api/schemas/).
 *
 * Hand-written rather than generated so the shapes stay reviewable, but they
 * are not invented: every field here exists in the served schema. Fields the
 * backend deliberately withholds -- retrieval score, source_path,
 * extracted_text, content_hash on search results, embeddings -- are absent
 * here too, so no component can render something the API does not return.
 */

export interface DepartmentRef {
  id: string
  name: string
}

export interface UserRef {
  id: string
  name: string | null
}

/** tags.id is SERIAL, the documented exception to "every id is a UUID". */
export interface TagRef {
  id: number
  name: string
}

export interface RevisionRef {
  revision_id: string
  revision_no: number
  created_at: string
}

/**
 * Citation anchor -- a discriminated union on `type`.
 *
 * A missing page number is the normal state for HWP/HWPX, whose formats do not
 * store one. `none` is an explicit member so a client branches instead of
 * probing for an undefined field, and nothing has to invent a page number.
 */
export type Anchor =
  | { type: 'paragraph'; paragraph_index: number; paragraph_end?: number | null }
  | { type: 'page'; page_number: number }
  | { type: 'none' }

export interface MatchedChunk {
  chunk_id: string
  revision_id: string
  section_title: string | null
  anchor: Anchor
}

export interface SearchItem {
  document_id: string
  title: string
  file_type: string
  department: DepartmentRef | null
  tags: TagRef[]
  updated_at: string
  current_revision: RevisionRef | null
  /** A newer revision exists but is not READY yet. Not a staleness warning. */
  has_newer_revision: boolean
  /** null in browse mode: no retrieval ran, so there is no matched text. */
  snippet: string | null
  matched_chunk: MatchedChunk | null

}

export interface SearchResponse {
  items: SearchItem[]
  page: number
  size: number
  total: number
}

export interface DocumentDetail {
  document_id: string
  title: string
  file_type: string
  department: DepartmentRef | null
  owner: UserRef | null
  tags: TagRef[]
  created_at: string
  updated_at: string
  /** The revision search actually serves. */
  current_revision: RevisionRef | null
  /** The newest revision discovered on disk; may still be processing. */
  latest_revision: RevisionRef | null
  is_searchable: boolean
  downloadable: boolean
}

export interface Revision {
  revision_id: string
  revision_no: number
  content_hash: string
  file_size: number | null
  source_modified_at: string | null
  /** Worker execution state: PENDING | RUNNING | SUCCESS | FAILED. */
  parse_status: string
  /** What the parse meant. SUCCESS + OCR_REQUIRED is a valid combination. */
  parse_result_code: string | null
  is_current: boolean
  is_ready: boolean
  created_at: string
}

export interface RevisionListResponse {
  items: Revision[]
  page: number
  size: number
  total: number
}

export interface TagListResponse {
  items: TagRef[]
  page: number
  size: number
  total: number
}

/** Contract section 11: departments are few, so there is no pagination. */
export interface DepartmentListResponse {
  items: DepartmentRef[]
}

/** The only error shape the API returns. */
export interface ErrorBody {
  code: string
  message: string
  request_id: string
  details?: { field?: string; reason?: string }[] | null
}

export interface ErrorResponse {
  error: ErrorBody
}

export const FILE_TYPES = ['hwp', 'hwpx', 'docx', 'pdf'] as const
export type FileType = (typeof FILE_TYPES)[number]

export const DEFAULT_PAGE_SIZE = 20

// ---------------------------------------------------------------------------
// Chat / RAG (contract section 9, src/api/schemas/chat.py)
// ---------------------------------------------------------------------------

export interface ChatSession {
  session_id: string
  /** Optional and often null: the API does not invent one from the question. */
  title: string | null
  created_at: string
  updated_at: string
}

export interface ChatSessionSummary extends ChatSession {
  message_count: number
}

export interface ChatSessionListResponse {
  items: ChatSessionSummary[]
  page: number
  size: number
  total: number
}

/**
 * A citation the caller may still read.
 *
 * Reuses the search UI's `Anchor`, so one helper renders positions everywhere
 * and no page number is invented for HWP/HWPX.
 */
export interface AccessibleSource {
  document_id: string
  revision_id: string
  chunk_id: string
  title: string
  file_type: string
  section_title: string | null
  anchor: Anchor
  accessible: true
}

/**
 * A citation whose document the caller can no longer read.
 *
 * The server withholds title, file_type, section_title and anchor entirely --
 * they are not null here, they are absent from the type, so no component can
 * render stale metadata it happens to still hold.
 */
export interface InaccessibleSource {
  document_id: string
  revision_id: string
  chunk_id: string
  accessible: false
}

/** Discriminated on `accessible`, matching the OpenAPI discriminator. */
export type ChatSource = AccessibleSource | InaccessibleSource

export interface PlainChatMessage {
  message_id: string
  role: 'user' | 'system'
  content: string
  created_at: string
}

export interface AssistantChatMessage {
  message_id: string
  role: 'assistant'
  /** null when the server hid it; read `content_hidden`, never infer from null. */
  content: string | null
  /** Server-owned. Never re-derived from the answer text or source count. */
  refused: boolean
  has_inaccessible_sources: boolean
  content_hidden: boolean
  sources: ChatSource[]
  created_at: string
}

export type ChatMessage = PlainChatMessage | AssistantChatMessage

export interface ChatMessagePage {
  items: ChatMessage[]
  page: number
  size: number
  total: number
}

export interface ChatSessionDetail extends ChatSession {
  messages: ChatMessagePage
}

export interface CreateSessionRequest {
  title?: string | null
}

export interface SendMessageRequest {
  message: string
}

/**
 * A freshly generated turn.
 *
 * `sources` is the accessible variant only: everything cited was re-checked
 * against the caller's permissions immediately before the answer was stored.
 */
export interface SendMessageResponse {
  message_id: string
  answer: string
  refused: boolean
  sources: AccessibleSource[]
  created_at: string
}

/** SendMessageRequest.message maxLength in the served schema. */
export const MESSAGE_MAX_LENGTH = 4000

export const CHAT_SESSION_PAGE_SIZE = 20
export const CHAT_MESSAGE_PAGE_SIZE = 50
