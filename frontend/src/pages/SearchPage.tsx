import { useCallback, useState } from 'react'
import { fetchDepartments, fetchTags } from '../api/metadata'
import { searchDocuments } from '../api/search'
import { AppNav } from '../components/AppNav'
import { Filters } from '../components/Filters'
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

  // Filter vocabulary comes from the server -- no department or tag name is
  // hard-coded in the client.
  const departments = useAsyncResource((signal) => fetchDepartments({ signal }), [])
  const tags = useAsyncResource((signal) => fetchTags({ signal }), [])

  const results = useAsyncResource(
    (signal) =>
      searchDocuments(
        {
          q: state.q,
          page: state.page,
          size: DEFAULT_PAGE_SIZE,
          departmentId: state.departmentId,
          year: state.year,
          tagIds: state.tagIds,
          fileType: state.fileType,
        },
        { signal },
      ),
    // An empty q is not an idle state: the backend browses by updated_at, so
    // the first visit shows accessible documents instead of a blank page.
    [state.q, state.page, state.departmentId, state.year, state.tagIds.join(','), state.fileType, submission],
  )

  const onSubmit = useCallback((q: string) => {
    if (q.trim() === state.q && state.page === 1) {
      setSubmission((value) => value + 1)
    } else {
      update({ q })
    }
  }, [update, state.q, state.page])
  const { data, error, loading } = results

  return (
    <main className="page">
      <AppNav />
      <h1 className="page-title">사내 문서 검색</h1>

      <SearchForm value={state.q} onSubmit={onSubmit} />

      <Filters
        state={state}
        departments={departments.data?.items ?? []}
        tags={tags.data?.items ?? []}
        departmentsLoading={departments.loading}
        tagsLoading={tags.loading}
        onChange={update}
      />

      {departments.error && <ErrorView error={departments.error} />}
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
    </main>
  )
}
