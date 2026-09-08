import { describe, expect, it } from 'vitest'
import {
  anchorLabel,
  formatFileSize,
  parseResultLabel,
  parseStatusLabel,
  revisionStateLabel,
} from './labels'
import { makeRevision } from './test/helpers'

describe('anchorLabel', () => {
  it('renders a single paragraph', () => {
    expect(anchorLabel({ type: 'paragraph', paragraph_index: 42, paragraph_end: null })).toBe('문단 42')
  })

  it('renders a paragraph range', () => {
    expect(anchorLabel({ type: 'paragraph', paragraph_index: 42, paragraph_end: 44 })).toBe('문단 42~44')
  })

  it('collapses a range whose end equals its start', () => {
    expect(anchorLabel({ type: 'paragraph', paragraph_index: 42, paragraph_end: 42 })).toBe('문단 42')
  })

  it('renders a page number only when one genuinely exists', () => {
    expect(anchorLabel({ type: 'page', page_number: 7 })).toBe('7페이지')
  })

  it('renders nothing for the none anchor rather than inventing a position', () => {
    // HWP/HWPX store no page numbers. "위치 없음" is the correct outcome, and
    // the caller must not draw a placeholder.
    expect(anchorLabel({ type: 'none' })).toBeNull()
    expect(anchorLabel(null)).toBeNull()
    expect(anchorLabel(undefined)).toBeNull()
  })

  it('prints the parser ordinal verbatim, with no renumbering', () => {
    expect(anchorLabel({ type: 'paragraph', paragraph_index: 0, paragraph_end: null })).toBe('문단 0')
  })
})

describe('status labels', () => {
  it('maps every parse_status in the schema CHECK constraint', () => {
    expect(parseStatusLabel('PENDING')).toBe('처리 대기')
    expect(parseStatusLabel('RUNNING')).toBe('처리 중')
    expect(parseStatusLabel('SUCCESS')).toBe('처리 완료')
    expect(parseStatusLabel('FAILED')).toBe('처리 실패')
  })

  it('maps every parse_result_code in the schema CHECK constraint', () => {
    for (const code of [
      'TEXT_EXTRACTED',
      'EMPTY_DOCUMENT',
      'OCR_REQUIRED',
      'ENCRYPTED',
      'CORRUPT',
      'UNSUPPORTED_FORMAT',
      'PARSE_FAILED',
    ]) {
      const label = parseResultLabel(code)
      expect(label).toBeTruthy()
      expect(label).not.toBe(code)
    }
    expect(parseResultLabel(null)).toBeNull()
  })

  it('passes an unknown code through instead of mislabelling it', () => {
    expect(parseStatusLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW')
  })
})

describe('revisionStateLabel', () => {
  it('marks the revision search actually serves', () => {
    expect(revisionStateLabel(makeRevision({ is_current: true }))).toBe('현재 검색 버전')
  })

  it('reports a failed parse', () => {
    expect(
      revisionStateLabel(makeRevision({ is_current: false, parse_status: 'FAILED', is_ready: false })),
    ).toBe('처리 실패')
  })

  it('keeps SUCCESS + OCR_REQUIRED distinguishable from a failure', () => {
    // The worker succeeded; the file is a scan. Merging these two axes would
    // tell the user their document failed when it did not.
    expect(
      revisionStateLabel(
        makeRevision({
          is_current: false,
          parse_status: 'SUCCESS',
          parse_result_code: 'OCR_REQUIRED',
          is_ready: false,
        }),
      ),
    ).toBe('처리 완료')
  })

  it('reports work still in progress', () => {
    expect(
      revisionStateLabel(makeRevision({ is_current: false, parse_status: 'RUNNING', is_ready: false })),
    ).toBe('처리 중')
  })
})

describe('formatFileSize', () => {
  it('formats sizes and returns null when unknown', () => {
    expect(formatFileSize(512)).toBe('512 B')
    expect(formatFileSize(20480)).toBe('20 KB')
    expect(formatFileSize(null)).toBeNull()
  })
})
