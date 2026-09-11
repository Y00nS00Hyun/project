import { useCallback, useState } from 'react'
import { fetchFolders } from '../api/folders'
import { fetchTags } from '../api/metadata'
import { searchDocuments } from '../api/search'
import { AppNav } from '../components/AppNav'
import { Filters } from '../components/Filters'
import { FolderTree } from '../components/FolderTree'
import { Pagination } from '../components/Pagination'
import { ResultCard } from '../components/ResultCard'
import { SearchForm } from '../components/SearchForm'
import { EmptyState, ErrorView, LoadingState } from '../components/StateViews'
import { useAsyncResource } from '../hooks/useAsyncResource'
import { useSearchState } from '../hooks/useSearchState'
import { DEFAULT_PAGE_SIZE } from '../api/types'

export function SearchPage() {
  const { state, update, goToPage } = useSearchState()
  const [submission, setSubmission] = useState(0)
  const [sidebarOpen, setSidebarOpen] = useState(true)

  // Filter vocabulary comes from the server -- no tag or document-kind name is
  // hard-coded in the client.
  //
  // Departments are deliberately not fetched: the organisation currently has a
  // single department, so a one-option filter is noise. The backend still
  // supports department filtering and ACL by department -- see
  // GET /api/v1/departments -- and re-enabling it is a UI change only.
  const tags = useAsyncResource((signal) => fetchTags({ signal }), [])

  // Fetched once, with no filter arguments. The tree is navigation: it must not
  // change shape when the user picks a year or a document kind, or a folder
  // would vanish while they were looking at it.
  const folders = useAsyncResource((signal) => fetchFolders({ signal }), [])

  const results = useAsyncResource(
    (signal) =>
      searchDocuments(
        {
          q: state.q,
          page: state.page,
          size: DEFAULT_PAGE_SIZE,
          year: state.year,
          tagIds: state.tagIds,
          fileType: state.fileType,
          folderPath: state.folderPath,
        },
        { signal },
      ),
    // An empty q is not an idle state: the backend browses by updated_at, so
    // the first visit shows accessible documents instead of a blank page.
    [state.q, state.page, state.year, state.tagIds.join(','), state.fileType,
     state.folderPath, submission],
  )

  const onSubmit = useCallback((q: string) => {
    if (q.trim() === state.q && state.page === 1) {
      setSubmission((value) => value + 1)
    } else {
      update({ q })
    }
  }, [update, state.q, state.page])
  const { data, error, loading } = results

  const selectedFolder =
    folders.data?.items.find((item) => item.path === state.folderPath) ?? null

  return (
    <main className="page page-with-sidebar">
      <AppNav />
      <h1 className="page-title">사내 문서 검색</h1>

      <div className="workspace">
        <aside className={sidebarOpen ? 'sidebar' : 'sidebar is-collapsed'}>
          <div className="sidebar-header">
            <h2 className="sidebar-title">프로젝트 / 폴더</h2>
            <button
              type="button"
              className="sidebar-toggle"
              aria-expanded={sidebarOpen}
              onClick={() => setSidebarOpen((open) => !open)}
            >
              {sidebarOpen ? '◀' : '▶'}
              <span className="visually-hidden">
                {sidebarOpen ? '폴더 목록 접기' : '폴더 목록 펼치기'}
              </span>
            </button>
          </div>
          {sidebarOpen && (
            <>
              {folders.error ? (
                <ErrorView error={folders.error} />
              ) : (
                <FolderTree
                  folders={folders.data?.items ?? []}
                  totalDocuments={folders.data?.total_documents ?? 0}
                  loading={folders.loading}
                  selected={state.folderPath}
                  // The canonical path is handed straight back; nothing here
                  // reconstructs it from display names.
                  onSelect={(path) => update({ folderPath: path })}
                />
              )}
            </>
          )}
        </aside>

        <div className="workspace-main">
      <SearchForm value={state.q} onSubmit={onSubmit} />

      <Filters
        state={state}
        tags={tags.data?.items ?? []}
        tagsLoading={tags.loading}
        onChange={update}
      />

      {selectedFolder && (
        <p className="selected-folder">
          <span className="selected-folder-label">폴더</span>
          {selectedFolder.name}
          <button type="button" className="chip chip-removable"
                  onClick={() => update({ folderPath: null })}
                  aria-label="폴더 선택 해제">
            해제 <span aria-hidden="true">×</span>
          </button>
        </p>
      )}

      {tags.error && <ErrorView error={tags.error} />}

      <section className="results" aria-busy={loading}>
        {error ? (
          <ErrorView error={error} />
        ) : loading && !data ? (
          <LoadingState label={state.q ? '검색 중...' : '문서를 불러오는 중...'} />
        ) : data ? (
          <>
            <p className="result-count">
              {state.q ? '검색 결과' : '문서'} {data.total.toLocaleString('ko-KR')}건
              {loading && <span className="inline-loading"> · 갱신 중...</span>}
            </p>
            {data.items.length === 0 ? (
              <EmptyState
                message={state.q ? '검색 결과가 없습니다.' : '표시할 문서가 없습니다.'}
              />
            ) : (
              <ul className="result-list">
                {data.items.map((item) => (
                  <ResultCard key={item.document_id} item={item} />
                ))}
              </ul>
            )}
            <Pagination page={data.page} size={data.size} total={data.total} onChange={goToPage} />
          </>
        ) : null}
      </section>
        </div>
      </div>
    </main>
  )
}
