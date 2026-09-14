import { useCallback, useState } from 'react'
import { ApiClientError } from '../api/client'
import { fetchDocumentDiff } from '../api/documents'
import type { RevisionDiffResponse } from '../api/types'
import { ErrorView, LoadingState } from './StateViews'

/**
 * What changed between the revision search serves and the one before it.
 *
 * Collapsed and unfetched until opened: most visitors never ask, and the
 * payload is document text. The comparison is paragraph by paragraph over the
 * extracted text -- no LLM, no character-level diff, and nothing about
 * formatting, which is why the notice below is always shown.
 */
export function RevisionDiff({ documentId }: { documentId: string }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<RevisionDiffResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<ApiClientError | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await fetchDocumentDiff(documentId))
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught
          : new ApiClientError('INTERNAL_ERROR', '변경사항을 불러오지 못했습니다.', 0, null),
      )
    } finally {
      setLoading(false)
    }
  }, [documentId])

  const toggle = useCallback(() => {
    setOpen((wasOpen) => {
      if (!wasOpen && !data && !loading) void load()
      return !wasOpen
    })
  }, [data, loading, load])

  return (
    <section className="section" aria-labelledby="revision-diff-heading">
      <h2 className="section-title" id="revision-diff-heading">
        <button type="button" className="disclosure" aria-expanded={open} onClick={toggle}>
          <span aria-hidden="true">{open ? '▾' : '▸'}</span> 이전 버전과 변경사항 보기
        </button>
      </h2>

      {open && (
        <>
          <p className="state-hint">
            추출된 텍스트를 기준으로 비교합니다. 서식, 레이아웃, 이미지 변경은 표시되지 않을 수 있습니다.
          </p>

          {loading && <LoadingState label="변경사항을 불러오는 중..." />}
          {error && <ErrorView error={error} />}

          {data && !data.comparable && (
            <p className="state-hint">비교 가능한 이전 버전이 없습니다.</p>
          )}

          {data?.comparable && data.base && data.target && (
            <div className="revision-diff">
              <p className="diff-range">
                Rev {data.base.revision_no} → Rev {data.target.revision_no}
              </p>

              {data.identical ? (
                <p className="state-hint">추출 텍스트 기준 변경사항이 없습니다.</p>
              ) : (
                <>
                  <DiffList title="추가된 내용" sign="+" kind="added"
                            items={data.added} total={data.added_total} />
                  <DiffList title="삭제된 내용" sign="-" kind="removed"
                            items={data.removed} total={data.removed_total} />
                  {data.truncated && (
                    <p className="state-hint">
                      변경된 문단이 많아 앞부분만 표시합니다.
                    </p>
                  )}
                </>
              )}
            </div>
          )}
        </>
      )}
    </section>
  )
}

function DiffList({ title, sign, kind, items, total }: {
  title: string
  sign: string
  kind: 'added' | 'removed'
  items: string[]
  total: number
}) {
  return (
    <div className={`diff-group diff-${kind}`}>
      <h3 className="diff-title">{title} ({total})</h3>
      {items.length === 0 ? (
        <p className="state-hint">없음</p>
      ) : (
        <ul className="diff-list">
          {items.map((text, index) => (
            // Plain text, never HTML: this is document content.
            <li key={index}>
              <span className="diff-sign" aria-hidden="true">{sign}</span>
              <span className="diff-text">{text}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
