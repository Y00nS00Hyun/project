import { Link, useLocation } from 'react-router-dom'
import type { SearchItem } from '../api/types'
import { anchorLabel, fileTypeLabel, formatDate } from '../labels'

/**
 * One document in the result list.
 *
 * There is no relevance number here, and there is nothing to derive one from:
 * the API does not return a score. That is deliberate on the backend side --
 * cosine and trigram similarity are incomparable scales, and the search PoC
 * measured a high cosine for a query with no real answer. Rendering any
 * percentage would be an invention.
 */
export function ResultCard({ item }: { item: SearchItem }) {
  const position = anchorLabel(item.matched_chunk?.anchor)
  const location = useLocation()
  const linkState = { search: location.search }

  return (
    <li className="result-card">
      <h3 className="result-title">
        <Link to={`/documents/${item.document_id}`} state={linkState}>{item.title}</Link>
      </h3>

      {/* Department is deliberately absent here too: a result line should
          carry what helps somebody choose between results, and every document
          would say the same thing. The field is still in the response. */}
      <p className="result-meta">
        <span>{fileTypeLabel(item.file_type)}</span>
        <span>수정 {formatDate(item.updated_at)}</span>
      </p>

      {/* Browse mode runs no retrieval, so snippet is null and no empty
          placeholder is drawn in its place. */}
      {item.snippet && (
        <p className="result-snippet">
          <q>{item.snippet}</q>
        </p>
      )}

      <p className="result-footer">
        {position && <span className="result-position">{position}</span>}
        {item.tags.map((tag) => (
          <span key={tag.id} className="chip">
            {tag.name}
          </span>
        ))}
        {item.has_newer_revision && (
          // "반영 중", not "미반영". Ingestion runs on a timer, so a newer
          // revision is on its way rather than stuck -- and the difference
          // decides whether somebody waits a minute or goes looking for an
          // administrator.
          <span
            className="chip chip-note"
            title="최신 파일 버전을 검색에 반영하는 중입니다. 그때까지는 현재 검색 버전이 사용됩니다"
          >
            새 버전 반영 중
          </span>
        )}
        <Link className="button button-quiet" to={`/documents/${item.document_id}`} state={linkState}>
          문서 보기
        </Link>
      </p>
    </li>
  )
}
