import type { Revision } from '../api/types'
import { formatDate, formatFileSize, parseResultLabel, revisionStateLabel } from '../labels'

/**
 * Version history.
 *
 * Backend enums are mapped to Korean wording without changing their meaning,
 * and parse_status is kept separate from parse_result_code: a revision can be
 * SUCCESS (the worker finished) and still not searchable (the file was a scan).
 */
export function RevisionList({ revisions }: { revisions: Revision[] }) {
  if (revisions.length === 0) return <p className="state">버전 이력이 없습니다.</p>

  return (
    <ul className="revision-list">
      {revisions.map((revision) => {
        const result = parseResultLabel(revision.parse_result_code)
        const size = formatFileSize(revision.file_size)
        return (
          <li
            key={revision.revision_id}
            className={revision.is_current ? 'revision is-current' : 'revision'}
          >
            <span className="revision-no">Rev {revision.revision_no}</span>
            <span className="revision-state">{revisionStateLabel(revision)}</span>
            <span className="revision-meta">
              {formatDate(revision.created_at)}
              {size ? ` · ${size}` : ''}
              {result && !revision.is_current ? ` · ${result}` : ''}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
