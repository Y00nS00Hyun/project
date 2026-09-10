import { FILE_TYPES, type DepartmentRef, type FileType, type TagRef } from '../api/types'
import { documentTypeOptions } from '../documentTypes'
import { fileTypeLabel } from '../labels'
import type { SearchState } from '../hooks/useSearchState'

interface Props {
  state: SearchState
  departments: DepartmentRef[]
  tags: TagRef[]
  departmentsLoading?: boolean
  tagsLoading?: boolean
  onChange: (patch: Partial<SearchState>) => void
}

/**
 * Years offered in the dropdown.
 *
 * There is no year-metadata endpoint and this task does not add one, so the
 * list is a recent range rather than the set of years that actually have
 * documents. A year already in the URL is folded in so a shared link keeps
 * showing its own filter.
 */
export function yearOptions(selected: number | null, now = new Date()): number[] {
  const current = now.getFullYear()
  const years: number[] = []
  for (let year = current; year >= current - 9; year -= 1) years.push(year)
  if (selected != null && !years.includes(selected)) {
    years.push(selected)
    years.sort((a, b) => b - a)
  }
  return years
}

export function Filters({ state, departments, tags, departmentsLoading, tagsLoading, onChange }: Props) {
  // Document kinds are tags under a reserved namespace, so they arrive on the
  // same GET /tags call and are filtered with the same tag_id parameter. The
  // namespace prefix is an implementation detail and never reaches the screen.
  const typeOptions = documentTypeOptions(tags)

  // Exactly one kind per document, so this is a single-select: the id of the
  // chosen kind is the only namespaced tag id in the URL.
  const typeIds = new Set(typeOptions.map((option) => option.id))
  const selectedTypeId = state.tagIds.find((id) => typeIds.has(id)) ?? null

  // Free-form tag ids stay in the URL untouched; changing the kind must not
  // drop a tag filter that some other control set.
  const otherTagIds = state.tagIds.filter((id) => !typeIds.has(id))

  const showTypeFilter = tagsLoading || typeOptions.length > 0

  const freeFormChips = otherTagIds.map(
    (id) => tags.find((tag) => tag.id === id) ?? { id, name: '목록에 없는 태그' },
  )

  return (
    <div className="filters">
      <div className="filter-row">
        <label className="filter">
          <span className="filter-label">부서</span>
          <select
            value={state.departmentId ?? ''}
            disabled={departmentsLoading}
            onChange={(event) => onChange({ departmentId: event.target.value || null })}
          >
            <option value="">전체</option>
            {state.departmentId && !departments.some((dept) => dept.id === state.departmentId) && (
              <option value={state.departmentId}>선택한 부서</option>
            )}
            {departments.map((department) => (
              <option key={department.id} value={department.id}>
                {department.name}
              </option>
            ))}
          </select>
        </label>

        <label className="filter">
          <span className="filter-label">연도</span>
          <select
            value={state.year == null ? '' : String(state.year)}
            onChange={(event) =>
              onChange({ year: event.target.value ? Number(event.target.value) : null })
            }
          >
            <option value="">전체</option>
            {yearOptions(state.year).map((year) => (
              <option key={year} value={year}>
                {year}년
              </option>
            ))}
          </select>
        </label>

        {showTypeFilter && (
        <label className="filter">
          <span className="filter-label">문서 종류</span>
          <select
            value={selectedTypeId == null ? '' : String(selectedTypeId)}
            disabled={tagsLoading}
            onChange={(event) => {
              const id = Number(event.target.value)
              // Single-select: replace the kind, keep every other tag filter.
              onChange({
                tagIds: event.target.value && Number.isInteger(id)
                  ? [...otherTagIds, id]
                  : otherTagIds,
              })
            }}
          >
            <option value="">전체</option>
            {typeOptions.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        )}

        <label className="filter">
          <span className="filter-label">파일 형식</span>
          <select
            value={state.fileType ?? ''}
            onChange={(event) =>
              onChange({ fileType: (event.target.value || null) as FileType | null })
            }
          >
            <option value="">전체</option>
            {FILE_TYPES.map((type) => (
              <option key={type} value={type}>
                {fileTypeLabel(type)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {/* Shown only once a year is actually chosen -- that is the moment the
          result count can drop to zero for a reason the user cannot see.
          The year comes from a four-digit number in the file name; a document
          whose name carries no year is deliberately left unlabelled rather
          than guessed at, so it cannot appear under any year. */}
      {state.year != null && (
        <p className="filter-hint">
          연도는 <strong>파일명에 적힌 4자리 연도</strong>로 판단합니다.
          파일명에 연도가 없는 문서는 어느 연도에도 포함되지 않습니다.
        </p>
      )}

      {/* Free-form tags only -- the document kind lives in its own dropdown.
          There is no UI to add these yet, but a shared link can carry one, and
          without a chip the recipient would have no way to clear it. */}
      {freeFormChips.length > 0 && (
        <ul className="chips" aria-label="선택한 태그">
          {freeFormChips.map((tag) => (
            <li key={tag.id}>
              <button
                type="button"
                className="chip chip-removable"
                onClick={() =>
                  onChange({ tagIds: state.tagIds.filter((id) => id !== tag.id) })
                }
                aria-label={`${tag.name} 태그 제거`}
              >
                {tag.name} <span aria-hidden="true">×</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
