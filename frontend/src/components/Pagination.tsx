interface Props {
  page: number
  size: number
  /** Document-level total, matching the document-level result list. */
  total: number
  onChange: (page: number) => void
  label?: string
}

/** Window of page numbers around the current page. */
export function pageWindow(page: number, pageCount: number, span = 5): number[] {
  if (pageCount <= 0) return []
  const half = Math.floor(span / 2)
  let start = Math.max(1, page - half)
  const end = Math.min(pageCount, start + span - 1)
  start = Math.max(1, end - span + 1)
  const pages: number[] = []
  for (let value = start; value <= end; value += 1) pages.push(value)
  return pages
}

export function Pagination({ page, size, total, onChange, label = '검색 결과 페이지' }: Props) {
  const pageCount = Math.ceil(total / size)
  if (pageCount <= 1 && page <= 1) return null

  return (
    <nav className="pagination" aria-label={label}>
      <button
        type="button"
        className="button button-quiet"
        onClick={() => onChange(page - 1)}
        disabled={page <= 1}
      >
        이전
      </button>
      {pageWindow(page, pageCount).map((value) => (
        <button
          key={value}
          type="button"
          className={value === page ? 'button page-button is-current' : 'button page-button'}
          aria-current={value === page ? 'page' : undefined}
          onClick={() => onChange(value)}
        >
          {value}
        </button>
      ))}
      <button
        type="button"
        className="button button-quiet"
        onClick={() => onChange(page + 1)}
        disabled={page >= pageCount}
      >
        다음
      </button>
    </nav>
  )
}
