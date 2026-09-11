import { useCallback, useState } from 'react'
import { ApiClientError } from '../api/client'
import { fetchDocumentText } from '../api/documents'
import { ErrorView, LoadingState } from './StateViews'
import type { TextBlock } from '../api/types'

const PAGE_SIZE = 20

/**
 * The text the parser extracted, on request.
 *
 * Collapsed and unfetched until someone opens it. A long report is hundreds of
 * thousands of characters, and it is not what most people came to the page for
 * -- loading it with the document would make every visit pay for a reading
 * nobody asked to do.
 *
 * Not a viewer. Tables, columns and page layout are gone; what is left is what
 * the parser could read, which is also exactly what search matches and what an
 * answer can cite. That equivalence is the reason to show it at all: it is how
 * a reader checks whether the system understood the document.
 */
export function TextPreview({ documentId }: { documentId: string }) {
  const [open, setOpen] = useState(false)
  const [blocks, setBlocks] = useState<TextBlock[]>([])
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<ApiClientError | null>(null)

  const load = useCallback(async (offset: number) => {
    if (loading) return
    setLoading(true)
    setError(null)
    try {
      const page = await fetchDocumentText(documentId, offset, PAGE_SIZE)
      // Appended, never replaced: "더 보기" continues the reading rather than
      // scrolling the reader back to the top of a fresh page.
      setBlocks((current) => (offset === 0 ? page.items : [...current, ...page.items]))
      setTotal(page.total)
      setHasMore(page.has_more)
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught
          : new ApiClientError('INTERNAL_ERROR', '원문을 불러오지 못했습니다.', 0, null),
      )
    } finally {
      setLoading(false)
    }
  }, [documentId, loading])

  const toggle = useCallback(() => {
    setOpen((wasOpen) => {
      // Fetched on first open only. Collapsing and reopening shows what was
      // already read instead of asking the server again.
      if (!wasOpen && blocks.length === 0 && !error) void load(0)
      return !wasOpen
    })
  }, [blocks.length, error, load])

  return (
    <section className="section" aria-labelledby="text-preview-heading">
      <h2 className="section-title" id="text-preview-heading">
        <button
          type="button"
          className="disclosure"
          aria-expanded={open}
          onClick={toggle}
        >
          <span aria-hidden="true">{open ? '▾' : '▸'}</span> 원문 텍스트 보기
        </button>
      </h2>

      {open && (
        <>
          <p className="state-hint">
            문서에서 추출한 텍스트입니다. 원본 서식이나 표 모양은 그대로 재현되지 않습니다.
            {total > 0 && ` 전체 ${total.toLocaleString('ko-KR')}개 블록 중 ${blocks.length}개.`}
          </p>

          {error && <ErrorView error={error} />}

          {blocks.length === 0 && loading && <LoadingState label="원문을 불러오는 중..." />}
          {blocks.length === 0 && !loading && !error && (
            <p className="state-hint">표시할 본문이 없습니다.</p>
          )}

          {blocks.length > 0 && (
            <div className="text-preview">
              {blocks.map((block) => (
                <article className="text-block" key={block.chunk_index}>
                  {block.section_title && (
                    <h3 className="text-block-section">{block.section_title}</h3>
                  )}
                  {/* pre-wrap: the extracted text carries the parser's own line
                      breaks, and collapsing them runs a table's cells together
                      into one unreadable line. */}
                  <p className="text-block-body">{block.text}</p>
                </article>
              ))}
            </div>
          )}

          {hasMore && (
            <button
              type="button"
              className="button"
              disabled={loading}
              onClick={() => void load(blocks.length)}
            >
              {loading ? '불러오는 중...' : '더 보기'}
            </button>
          )}
        </>
      )}
    </section>
  )
}
