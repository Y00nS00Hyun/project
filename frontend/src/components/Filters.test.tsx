import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Filters, yearOptions } from './Filters'
import { renderAt } from '../test/helpers'
import type { SearchState } from '../hooks/useSearchState'

const EMPTY: SearchState = {
  q: '',
  page: 1,
  departmentId: null,
  year: null,
  tagIds: [],
  fileType: null,
}

function render(state: Partial<SearchState> = {}, tags = [{ id: 12, name: '보안' }]) {
  const onChange = vi.fn()
  renderAt(
    <Filters
      state={{ ...EMPTY, ...state }}
      departments={[{ id: 'dep-1', name: '기획조정실' }]}
      tags={tags}
      onChange={onChange}
    />,
  )
  return onChange
}

describe('year filter', () => {
  it('defaults to 전체, never to a concrete year', () => {
    // A year silently pre-selected would filter the list on first load and the
    // user would see an empty result set they never asked for.
    render()
    expect(screen.getByLabelText('연도')).toHaveValue('')
  })

  it('shows the chosen year when one is set', () => {
    render({ year: 2026 })
    expect(screen.getByLabelText('연도')).toHaveValue('2026')
  })

  it('keeps a year from a shared link even when outside the recent range', () => {
    render({ year: 2011 })
    expect(screen.getByLabelText('연도')).toHaveValue('2011')
    expect(yearOptions(2011, new Date('2026-09-09'))).toContain(2011)
  })

  it('explains the filename basis only once a year is chosen', () => {
    // Silent on the default view; the note appears at the moment the result
    // count can drop for a reason the user cannot see.
    render()
    expect(screen.queryByText(/파일명에 적힌/)).not.toBeInTheDocument()

    render({ year: 2026 })
    expect(screen.getByText(/파일명에 적힌/)).toBeInTheDocument()
    expect(screen.getByText(/어느 연도에도 포함되지 않습니다/)).toBeInTheDocument()
  })

  it('reports the selected year to the caller', async () => {
    const onChange = render()
    await userEvent.selectOptions(screen.getByLabelText('연도'), '2026')
    expect(onChange).toHaveBeenCalledWith({ year: 2026 })
  })
})

describe('document type filter', () => {
  const TYPES = [
    { id: 1, name: '종류:매뉴얼' },
    { id: 2, name: '종류:요구사항 정의서' },
    { id: 3, name: '종류:제안·입찰 문서' },
    { id: 4, name: '종류:보고서' },
    { id: 5, name: '종류:기타' },
  ]

  it('never shows the namespace prefix to the user', () => {
    render({}, TYPES)
    for (const label of ['매뉴얼', '요구사항 정의서', '제안·입찰 문서', '보고서', '기타']) {
      expect(screen.getByRole('option', { name: label })).toBeInTheDocument()
    }
    expect(screen.queryByText(/종류:/)).not.toBeInTheDocument()
  })

  it('orders the kinds deliberately, with 기타 last', () => {
    render({}, [...TYPES].reverse())
    const options = Array.from(
      (screen.getByLabelText('문서 종류') as HTMLSelectElement).options,
    ).map((o) => o.text)
    expect(options).toEqual(['전체', '매뉴얼', '요구사항 정의서', '제안·입찰 문서', '보고서', '기타'])
  })

  it('defaults to 전체', () => {
    render({}, TYPES)
    expect(screen.getByLabelText('문서 종류')).toHaveValue('')
  })

  it('sends the chosen kind as a tag id', async () => {
    const onChange = render({}, TYPES)
    await userEvent.selectOptions(screen.getByLabelText('문서 종류'), '4')
    expect(onChange).toHaveBeenCalledWith({ tagIds: [4] })
  })

  it('replaces rather than accumulates: one kind per document', async () => {
    // Two kind ids in the URL would AND together and always return nothing.
    const onChange = render({ tagIds: [1] }, TYPES)
    await userEvent.selectOptions(screen.getByLabelText('문서 종류'), '4')
    expect(onChange).toHaveBeenCalledWith({ tagIds: [4] })
  })

  it('clearing the kind removes it from the filter', async () => {
    const onChange = render({ tagIds: [1] }, TYPES)
    await userEvent.selectOptions(screen.getByLabelText('문서 종류'), '')
    expect(onChange).toHaveBeenCalledWith({ tagIds: [] })
  })

  it('shows the kind already pinned in the URL', () => {
    render({ tagIds: [2] }, TYPES)
    expect(screen.getByLabelText('문서 종류')).toHaveValue('2')
  })

  it('keeps free-form tag ids when the kind changes', async () => {
    // A free-form tag id from a shared link must survive a kind change.
    const onChange = render({ tagIds: [99, 1] }, [...TYPES, { id: 99, name: '보안' }])
    await userEvent.selectOptions(screen.getByLabelText('문서 종류'), '4')
    expect(onChange).toHaveBeenCalledWith({ tagIds: [99, 4] })
  })

  it('shows free-form tags as removable chips, not as kinds', () => {
    render({ tagIds: [99] }, [...TYPES, { id: 99, name: '보안' }])
    expect(screen.getByRole('button', { name: /보안 태그 제거/ })).toBeInTheDocument()
    expect(screen.getByLabelText('문서 종류')).toHaveValue('')
  })

  it('is hidden when the server returns no kinds at all', () => {
    render({}, [])
    expect(screen.queryByLabelText('문서 종류')).not.toBeInTheDocument()
  })

  it('does not hide the other filters', () => {
    render({}, [])
    expect(screen.getByLabelText('부서')).toBeInTheDocument()
    expect(screen.getByLabelText('연도')).toBeInTheDocument()
    expect(screen.getByLabelText('파일 형식')).toBeInTheDocument()
  })
})
