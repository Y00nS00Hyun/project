import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { ApiClientError } from '../api/client'
import { downloadDocument, fetchDocument, fetchRevisions } from '../api/documents'
import { DocumentChat } from '../components/DocumentChat'
import { DocumentSummary } from '../components/DocumentSummary'
import { RevisionList } from '../components/RevisionList'
import { TextPreview } from '../components/TextPreview'
import { Pagination } from '../components/Pagination'
import { ErrorView, LoadingState } from '../components/StateViews'
import { useAsyncResource } from '../hooks/useAsyncResource'
import { fileTypeLabel, formatDate, formatDateOnly, formatFileSize } from '../labels'

export function DocumentPage() {
  const { documentId = '' } = useParams()
  return <DocumentContent key={documentId} documentId={documentId} />
}

function DocumentContent({ documentId }: { documentId: string }) {
  const [revisionPage, setRevisionPage] = useState(1)
  const [historyOpen, setHistoryOpen] = useState(false)

  const detail = useAsyncResource(
    (signal) => fetchDocument(documentId, { signal }),
    [documentId],
    Boolean(documentId),
  )
  const revisions = useAsyncResource(
    (signal) => fetchRevisions(documentId, revisionPage, 20, { signal }),
    [documentId, revisionPage],
    Boolean(documentId),
  )

  const [downloadError, setDownloadError] = useState<ApiClientError | null>(null)
  const [downloading, setDownloading] = useState(false)
  const downloadController = useRef<AbortController | null>(null)
  useEffect(() => () => downloadController.current?.abort(), [])

  const onDownload = useCallback(async () => {
    if (downloadController.current) return
    const controller = new AbortController()
    downloadController.current = controller
    setDownloadError(null)
    setDownloading(true)
    try {
      // Only the document id goes out. The filename comes back in
      // Content-Disposition; the client never sees or builds a path.
      const fallback = `${detail.data?.title ?? 'document'}.${detail.data?.file_type ?? 'bin'}`
      const file = await downloadDocument(documentId, fallback, { signal: controller.signal })
      if (controller.signal.aborted) return
      const url = URL.createObjectURL(file.blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = file.filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      if (controller.signal.aborted) return
      setDownloadError(
        error instanceof ApiClientError
          ? error
          : new ApiClientError('INTERNAL_ERROR', '다운로드에 실패했습니다.', 0, null),
      )
    } finally {
      downloadController.current = null
      if (!controller.signal.aborted) setDownloading(false)
    }
  }, [documentId, detail.data?.title, detail.data?.file_type])

  if (detail.error) {
    // 404 covers both "no such document" and "no permission" -- the backend
    // returns the same status for each so existence is not disclosed. The UI
    // must not guess which one it was.
    if (detail.error.isNotFound) {
      return (
        <main className="page">
          <BackLink />
          <div className="state state-error" role="alert">
            <p className="state-message">문서를 찾을 수 없습니다.</p>
          </div>
        </main>
      )
    }
    return (
      <main className="page">
        <BackLink />
        <ErrorView error={detail.error} />
      </main>
    )
  }

  if (!detail.data) {
    return (
      <main className="page">
        <BackLink />
        <LoadingState label="문서를 불러오는 중..." />
      </main>
    )
  }

  const doc = detail.data
  const current = doc.current_revision
  const latest = doc.latest_revision
  // Requires a current revision, not merely a different one. A document whose
  // first revision is still being processed has nothing older to fall back to,
  // and saying search is "using the previous version" would be false -- that
  // case is covered by the 검색할 본문 없음 notice below.
  const newerPending = current != null && latest != null && latest.revision_id !== current.revision_id

  return (
    <main className="page">
      <BackLink />
      <h1 className="page-title">{doc.title}</h1>

      {/* No department row. The organisation does not use departments, so the
          field could only ever read "-" or name something nobody navigates by.
          documents.department_id, the departments table, the department ACL
          principal and GET /departments are all untouched -- and so is
          DocumentDetail.department in the response, because removing a field
          from a served schema is a breaking change for no gain. */}
      <dl className="detail-grid">
        <Field label="파일 형식" value={fileTypeLabel(doc.file_type)} />
        {/* Three different dates, so each says which one it is.

            작성일  what the document's own cover states, or 알 수 없음
            등록일  when this system first saw the file
            수정일  the file's own mtime in the shared folder

            수정일 used to show documents.updated_at, which is when our
            pipeline last touched the row -- it moved when a revision was
            promoted, which is not something a reader did or would recognise. */}
        <Field label="문서 작성일" value={formatDateOnly(doc.document_date)} />
        <Field label="시스템 등록일" value={formatDate(doc.created_at)} />
        <Field
          label="원본 파일 수정일"
          value={doc.source_modified_at ? formatDate(doc.source_modified_at) : '알 수 없음'}
        />
        <Field
          label="파일 크기"
          value={formatFileSize(doc.file_size) ?? '알 수 없음'}
        />
        <Field
          label="검색에 사용 중인 버전"
          value={current ? `Rev ${current.revision_no} (${formatDate(current.created_at)})` : '검색 불가'}
        />
        {/* "최신 감지 버전" appears only while the two disagree. When they
            agree it was the same number printed twice, which is what got it
            removed from the grid; while they disagree it is the one figure
            that tells a reader their edit was seen but is not searchable
            yet. */}
        {newerPending && latest && (
          <div className="detail-field">
            <dt>최신 감지 버전</dt>
            <dd>
              Rev {latest.revision_no} ({formatDate(latest.created_at)}){' '}
              <span className="chip chip-processing">처리 중</span>
            </dd>
          </div>
        )}
      </dl>

      {doc.tags.length > 0 && (
        <ul className="chips" aria-label="태그">
          {doc.tags.map((tag) => (
            <li key={tag.id}>
              <Link className="chip" to={`/search?tag_id=${tag.id}`}>
                {tag.name}
              </Link>
            </li>
          ))}
        </ul>
      )}

      {/* Not an error, and not something the reader can act on: ingestion runs
          on a timer and this clears itself. It is here because a search that
          quietly answers from last week's copy of a file the reader edited
          this morning is the kind of wrong that goes unnoticed. */}
      {newerPending && (
        <p className="version-warning" role="status">
          <span aria-hidden="true">⚠</span> 최신 파일을 처리 중입니다. 현재 검색에는 이전 버전이 사용되고 있습니다.
        </p>
      )}
      {!doc.is_searchable && (
        <p className="notice">이 문서는 아직 검색에 사용할 수 있는 본문이 없습니다.</p>
      )}

      <DocumentSummary summary={doc.summary} />

      {doc.is_searchable && (
        // Only when there is a current revision to answer from. Offering the
        // box for a document with no searchable body would invite questions
        // that can only be refused.
        //
        // Rendered even when the capability is off: the panel then explains
        // that the feature is disabled, which is more use than a section that
        // silently is not there.
        <DocumentChat
          documentId={doc.document_id}
          title={doc.title}
          available={doc.chat.available}
        />
      )}

      <section className="section">
        <h2 className="section-title">원본 파일</h2>
        <button
          type="button"
          className="button button-primary"
          onClick={onDownload}
          disabled={!doc.downloadable || downloading}
        >
          {downloading ? '내려받는 중...' : '원본 다운로드'}
        </button>
        {!doc.downloadable && (
          <p className="state-hint">현재 이 문서의 원본을 내려받을 수 없습니다.</p>
        )}
        {downloadError && <ErrorView error={downloadError} />}
      </section>

      {doc.is_searchable && <TextPreview documentId={doc.document_id} />}

      {/* Most documents are ingested once and never edited, and for those the
          history is a single row restating the page above it. Hidden there,
          and collapsed -- not hidden -- once there is an actual sequence to
          look through. Revisions themselves are untouched: this is which rows
          are drawn, not which rows exist. */}
      {revisions.error ? (
        <section className="section">
          <h2 className="section-title">버전 이력</h2>
          <ErrorView error={revisions.error} />
        </section>
      ) : revisions.data && revisions.data.total > 1 ? (
        <section className="section">
          <h2 className="section-title">
            <button
              type="button"
              className="disclosure"
              aria-expanded={historyOpen}
              onClick={() => setHistoryOpen((open) => !open)}
            >
              <span aria-hidden="true">{historyOpen ? '▾' : '▸'}</span> 버전 이력 ({revisions.data.total})
            </button>
          </h2>
          {historyOpen && (
            <>
              <RevisionList revisions={revisions.data.items} />
              <Pagination
                page={revisions.data.page}
                size={revisions.data.size}
                total={revisions.data.total}
                onChange={setRevisionPage}
                label="버전 이력 페이지"
              />
            </>
          )}
        </section>
      ) : null}
    </main>
  )
}

function BackLink() {
  const location = useLocation()
  const state: unknown = location.state
  const search = state && typeof state === 'object' && 'search' in state
    && typeof state.search === 'string' && state.search.startsWith('?') ? state.search : ''
  return (
    <p className="back-link">
      <Link to={`/search${search}`}>← 검색으로</Link>
    </p>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-field">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}
