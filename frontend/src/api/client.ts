import type { ErrorResponse } from './types'

/**
 * The single place that talks HTTP.
 *
 * Base URL comes from the environment (default "/api/v1", which the Vite dev
 * proxy forwards to FastAPI) so no host is hard-coded in components.
 */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

/** Header the backend's development identity provider reads. */
const DEBUG_USER_HEADER = 'X-Debug-User-Id'

/**
 * An error the API reported in its envelope, or a transport failure.
 *
 * `code` is the contract's closed set (UNAUTHENTICATED, DOCUMENT_NOT_FOUND,
 * VALIDATION_ERROR, ...). `message` is server-authored and safe to show;
 * nothing else from the response is surfaced to the user.
 */
export class ApiClientError extends Error {
  readonly code: string
  readonly status: number
  readonly requestId: string | null

  constructor(code: string, message: string, status: number, requestId: string | null) {
    super(message)
    this.name = 'ApiClientError'
    this.code = code
    this.status = status
    this.requestId = requestId
  }

  get isUnauthenticated(): boolean {
    return this.code === 'UNAUTHENTICATED' || this.status === 401
  }

  get isNotFound(): boolean {
    return this.status === 404
  }
}

/**
 * Development identity.
 *
 * Real SSO is not built yet, so outside production the backend accepts an
 * identity header. Two guards keep it out of a deployed bundle:
 *
 *   1. `import.meta.env.DEV` is statically replaced with `false` in a
 *      production build, so this whole branch is removed as dead code.
 *   2. The value comes from .env.local, never from a UI field -- there is no
 *      form anywhere in this app that lets a user type a user id.
 *
 * The backend refuses the header in production regardless, so a stale build
 * cannot impersonate anyone either.
 */
function identityHeaders(): Record<string, string> {
  if (!import.meta.env.DEV) return {}
  const debugUserId = import.meta.env.VITE_DEBUG_USER_ID
  return debugUserId ? { [DEBUG_USER_HEADER]: debugUserId } : {}
}

export type QueryValue = string | number | boolean | null | undefined
export type QueryParams = Record<string, QueryValue | QueryValue[]>

export function buildQuery(params: QueryParams): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue
    // Repeated keys, not a comma-joined string: tag_id is AND-combined by the
    // backend and must arrive as separate parameters.
    if (Array.isArray(value)) {
      for (const entry of value) {
        if (entry === null || entry === undefined || entry === '') continue
        search.append(key, String(entry))
      }
      continue
    }
    search.append(key, String(value))
  }
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

async function toApiError(response: Response): Promise<ApiClientError> {
  const requestId = response.headers.get('X-Request-Id')
  try {
    const body = (await response.json()) as Partial<ErrorResponse>
    const error = body?.error
    if (error?.code && error?.message) {
      return new ApiClientError(error.code, error.message, response.status, error.request_id ?? requestId)
    }
  } catch {
    // Not JSON -- a proxy or gateway error page. Fall through; the raw body is
    // never shown to the user.
  }
  return new ApiClientError(
    'INTERNAL_ERROR',
    '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.',
    response.status,
    requestId,
  )
}

export interface RequestOptions {
  params?: QueryParams
  signal?: AbortSignal
}

async function fetchApi(path: string, options: RequestOptions = {}): Promise<Response> {
  const url = `${API_BASE_URL}${path}${buildQuery(options.params ?? {})}`
  let response: Response
  try {
    response = await fetch(url, {
      method: 'GET',
      credentials: 'include',
      headers: { Accept: 'application/json', ...identityHeaders() },
      signal: options.signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiClientError('NETWORK_ERROR', '서버에 연결할 수 없습니다.', 0, null)
  }
  if (!response.ok) throw await toApiError(response)
  return response
}

export async function getJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await fetchApi(path, options)
  return (await response.json()) as T
}

/** A file the API returned, already read into memory for saving. */
export interface DownloadedFile {
  blob: Blob
  filename: string
}

const FILENAME_STAR = /filename\*=UTF-8''([^;]+)/i
const FILENAME_PLAIN = /filename="?([^";]+)"?/i

/**
 * Read the display filename out of Content-Disposition.
 *
 * This is the only filename the client ever sees. The response carries no
 * filesystem path -- the backend resolves the shared-folder location itself
 * and sends bytes plus a display name.
 */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback
  const encoded = FILENAME_STAR.exec(header)
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1])
    } catch {
      /* fall through to the plain form */
    }
  }
  return FILENAME_PLAIN.exec(header)?.[1] ?? fallback
}

export async function getFile(
  path: string,
  fallbackName: string,
  options: RequestOptions = {},
): Promise<DownloadedFile> {
  const response = await fetchApi(path, options)
  return {
    blob: await response.blob(),
    filename: filenameFromDisposition(response.headers.get('Content-Disposition'), fallbackName),
  }
}
