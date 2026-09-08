import { FILE_TYPES, type DepartmentRef, type FileType, type TagRef } from '../api/types'
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
  const selectedTags = state.tagIds.map((id) => tags.find((tag) => tag.id === id)
    ?? { id, name: '목록에 없는 태그' })
  const available = tags.filter((tag) => !state.tagIds.includes(tag.id))

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

        <label className="filter">
          <span className="filter-label">태그</span>
          <select
            value=""
            disabled={tagsLoading}
            onChange={(event) => {
              const id = Number(event.target.value)
              // Multiple tags are AND-combined by the backend, so each pick
              // narrows rather than replaces.
              if (Number.isInteger(id) && id) onChange({ tagIds: [...state.tagIds, id] })
            }}
          >
            <option value="">추가</option>
            {available.map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.name}
              </option>
            ))}
          </select>
        </label>

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

      {selectedTags.length > 0 && (
        <ul className="chips" aria-label="선택한 태그">
          {selectedTags.map((tag) => (
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
