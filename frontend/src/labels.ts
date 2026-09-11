import type { Anchor, Revision } from './api/types'

/**
 * Turn an anchor into a short position label.
 *
 * The index is printed exactly as the parser reported it -- no +1, no
 * renumbering. It is the parser's own paragraph ordinal, and adjusting it here
 * would invent a numbering the document does not have. Returns null for
 * `none`, so the caller renders nothing rather than an empty placeholder.
 */
export function anchorLabel(anchor: Anchor | null | undefined): string | null {
  if (!anchor) return null
  switch (anchor.type) {
    case 'paragraph':
      return anchor.paragraph_end != null && anchor.paragraph_end !== anchor.paragraph_index
        ? `문단 ${anchor.paragraph_index}~${anchor.paragraph_end}`
        : `문단 ${anchor.paragraph_index}`
    case 'page':
      return `${anchor.page_number}페이지`
    case 'none':
      // HWP/HWPX carry no page numbers. Absent position is normal, not an error.
      return null
  }
}

/** Worker execution state. Meaning is preserved; only the wording is localised. */
const PARSE_STATUS_LABELS: Record<string, string> = {
  PENDING: '처리 대기',
  RUNNING: '처리 중',
  SUCCESS: '처리 완료',
  FAILED: '처리 실패',
}

/**
 * What the parse *meant*, which is a separate axis from whether the worker ran.
 * SUCCESS + OCR_REQUIRED is a normal pair: the parser correctly determined the
 * file is a scan. So these are never merged into one status.
 */
const PARSE_RESULT_LABELS: Record<string, string> = {
  TEXT_EXTRACTED: '본문 추출됨',
  EMPTY_DOCUMENT: '본문 없음',
  OCR_REQUIRED: '이미지 문서(OCR 필요)',
  ENCRYPTED: '암호 걸린 문서',
  CORRUPT: '파일 손상',
  UNSUPPORTED_FORMAT: '지원하지 않는 형식',
  PARSE_FAILED: '본문 추출 실패',
}

export function parseStatusLabel(status: string): string {
  return PARSE_STATUS_LABELS[status] ?? status
}

export function parseResultLabel(code: string | null): string | null {
  if (!code) return null
  return PARSE_RESULT_LABELS[code] ?? code
}

/**
 * One line describing a revision's state.
 *
 * "현재 검색 버전" is about which revision search serves, which is not the same
 * as the newest revision on disk -- a newer one may still be processing or may
 * have failed.
 */
export function revisionStateLabel(revision: Revision): string {
  if (revision.is_current) return '현재 검색 버전'
  if (revision.parse_status === 'FAILED') return '처리 실패'
  return parseStatusLabel(revision.parse_status)
}

export function fileTypeLabel(fileType: string): string {
  return fileType.toUpperCase()
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '-'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '-'
  return new Intl.DateTimeFormat('ko-KR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date)
}

export function formatFileSize(bytes: number | null | undefined): string | null {
  if (bytes == null) return null
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}


/**
 * A calendar date with no time, as the document itself printed it.
 *
 * Parsed as a plain Y/M/D rather than through `new Date(value)`: an ISO date
 * with no time is read as UTC midnight, which renders as the previous day for
 * anyone west of Greenwich. The document states a day; showing a different one
 * would be this code introducing an error the document does not contain.
 */
export function formatDateOnly(value: string | null | undefined): string {
  if (!value) return '알 수 없음'
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return '알 수 없음'
  const [, year, month, day] = match
  return `${year}. ${Number(month)}. ${Number(day)}.`
}
