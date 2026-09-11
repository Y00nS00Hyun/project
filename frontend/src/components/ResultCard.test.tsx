import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { ResultCard } from './ResultCard'
import { makeItem, renderAt } from '../test/helpers'

function renderCard(item = makeItem()) {
  return renderAt(
    <ul>
      <ResultCard item={item} />
    </ul>,
  )
}

describe('ResultCard', () => {
  it('shows title, file type and snippet', () => {
    renderCard()
    expect(screen.getByRole('link', { name: '2026년 AI 문서관리 사업계획서' })).toHaveAttribute(
      'href',
      '/documents/doc-1',
    )
    expect(screen.getByText('HWPX')).toBeInTheDocument()
    expect(screen.getByText(/총 사업비는 300,000,000원이며/)).toBeInTheDocument()
  })

  it('does not show the document department', () => {
    // The organisation does not use departments, so every result would carry
    // the same word -- a meta line should help somebody choose between
    // results. The field is still in the response and the fixture still sends
    // it, so this fails if the rendering comes back.
    renderCard()
    expect(screen.queryByText('기획조정실')).not.toBeInTheDocument()
  })

  it('shows the matched position from the anchor', () => {
    renderCard()
    expect(screen.getByText('문단 42')).toBeInTheDocument()
  })

  it('shows no position when the parser recovered none', () => {
    renderCard(
      makeItem({
        matched_chunk: {
          chunk_id: 'c',
          revision_id: 'r',
          section_title: null,
          anchor: { type: 'none' },
        },
      }),
    )
    expect(screen.queryByText(/문단/)).not.toBeInTheDocument()
    expect(screen.queryByText(/페이지/)).not.toBeInTheDocument()
  })

  it('draws no snippet placeholder in browse mode', () => {
    const { container } = renderCard(makeItem({ snippet: null, matched_chunk: null }))
    expect(container.querySelector('.result-snippet')).toBeNull()
  })

  it('flags a newer revision that has not been indexed yet', () => {
    renderCard(makeItem({ has_newer_revision: true }))
    expect(screen.getByText('새 버전 미반영')).toBeInTheDocument()
  })

  it('renders no relevance number anywhere', () => {
    // The API returns no score. Nothing in the card may look like one.
    const { container } = renderCard()
    const text = container.textContent ?? ''
    expect(text).not.toMatch(/%/)
    expect(text).not.toMatch(/정확도|유사도|score|confidence/i)
  })

  it('omits the year chip while the API does not return a year', () => {
    // Contract section 6.5 has no year field. The card must not fabricate one
    // from updated_at.
    const { container } = renderCard(makeItem({ updated_at: '2026-08-30T04:12:00Z' }))
    expect(container.querySelector('.result-meta')?.textContent).not.toContain('2026년')
  })
})
