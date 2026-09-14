import { FILE_TYPES, type FileType, type TagRef } from "../api/types";
import { documentTypeOptions } from "../documentTypes";
import { fileTypeLabel } from "../labels";
import type { SearchState } from "../hooks/useSearchState";

interface Props {
  state: SearchState;
  tags: TagRef[];
  tagsLoading?: boolean;
  /** Years from GET /search/years. Empty while loading or if that call failed. */
  years?: readonly number[];
  onChange: (patch: Partial<SearchState>) => void;
}

// No department filter.
//
// The organisation runs a single department today, so the control could only
// ever offer one choice. Everything behind it is kept: departments still exist
// in the schema, document_permissions still grants by department, the ACL query
// still resolves it, and GET /api/v1/search still accepts department_id.
// Bringing the filter back is a change to this file alone.

/**
 * Years offered in the dropdown, newest first.
 *
 * Only what the server reported: the years documents this user may search
 * actually carry. Nothing is generated from the calendar, so an old document
 * stays reachable and an empty year is never offered.
 *
 * A year already in the URL is folded in even when the server did not list
 * it -- a shared link, or a list that failed to load -- so the selection the
 * page is showing results for does not silently vanish from the control.
 */
export function yearOptions(
  available: readonly number[],
  selected: number | null,
): number[] {
  const years = new Set(available.filter((year) => Number.isInteger(year)));
  if (selected != null) years.add(selected);
  return [...years].sort((a, b) => b - a);
}

export function Filters({ state, tags, tagsLoading, years = [], onChange }: Props) {
  // Document kinds are tags under a reserved namespace, so they arrive on the
  // same GET /tags call and are filtered with the same tag_id parameter. The
  // namespace prefix is an implementation detail and never reaches the screen.
  const typeOptions = documentTypeOptions(tags);

  // Exactly one kind per document, so this is a single-select: the id of the
  // chosen kind is the only namespaced tag id in the URL.
  const typeIds = new Set(typeOptions.map((option) => option.id));
  const selectedTypeId = state.tagIds.find((id) => typeIds.has(id)) ?? null;

  // Free-form tag ids stay in the URL untouched; changing the kind must not
  // drop a tag filter that some other control set.
  const otherTagIds = state.tagIds.filter((id) => !typeIds.has(id));

  const showTypeFilter = tagsLoading || typeOptions.length > 0;

  const freeFormChips = otherTagIds.map(
    (id) =>
      tags.find((tag) => tag.id === id) ?? { id, name: "목록에 없는 태그" },
  );

  return (
    <div className="filters">
      <div className="filter-row">
        <label className="filter">
          <span className="filter-label">연도</span>
          <select
            value={state.year == null ? "" : String(state.year)}
            onChange={(event) =>
              onChange({
                year: event.target.value ? Number(event.target.value) : null,
              })
            }
          >
            <option value="">전체</option>
            {yearOptions(years, state.year).map((year) => (
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
              value={selectedTypeId == null ? "" : String(selectedTypeId)}
              disabled={tagsLoading}
              onChange={(event) => {
                const id = Number(event.target.value);
                // Single-select: replace the kind, keep every other tag filter.
                onChange({
                  tagIds:
                    event.target.value && Number.isInteger(id)
                      ? [...otherTagIds, id]
                      : otherTagIds,
                });
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
            value={state.fileType ?? ""}
            onChange={(event) =>
              onChange({
                fileType: (event.target.value || null) as FileType | null,
              })
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
          The year comes from an explicit date on the document's cover, falling
          back to the file name; a document that states neither is left
          unlabelled rather than guessed at, so it appears under no year. */}
      {state.year != null && (
        <p className="filter-hint">
          연도는 <strong>문서 표지·앞부분의 날짜</strong>를 우선 기준으로
          판단하고, 없으면 파일명의 연도를 사용합니다. <br />
          어느 쪽에도 연도가 없는 문서는 어느 연도에도 포함되지 않습니다.
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
                  onChange({
                    tagIds: state.tagIds.filter((id) => id !== tag.id),
                  })
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
  );
}
