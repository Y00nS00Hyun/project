import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DocumentSummary } from './DocumentSummary'
import type { DocumentSummary as Summary } from '../api/types'

function summary(overrides: Partial<Summary> = {}): Summary {
  return {
    state: 'SUCCESS',
    reason: null,
    content: '이 문서는 서버 장애 대응 절차를 설명합니다.',
    generated_at: '2026-09-01T00:00:00Z',
    available: true,
    revision_id: 'rev-2',
    ...overrides,
  }
}

describe('DocumentSummary', () => {
  it('shows the stored summary and says it was generated', () => {
    render(<DocumentSummary summary={summary()} />)
    expect(screen.getByText(/서버 장애 대응 절차를 설명합니다/)).toBeInTheDocument()
    expect(screen.getByText(/자동 생성된 요약/)).toBeInTheDocument()
  })

  it.each(['PENDING', 'RUNNING'] as const)('says a %s summary is on its way', (state) => {
    render(<DocumentSummary summary={summary({ state, reason: null, content: null })} />)
    expect(screen.getByText(/생성하고 있습니다/)).toBeInTheDocument()
  })

  it('does not promise a summary when the deployment generates none', () => {
    // The point of the capability field. A provider-off deployment leaves
    // revisions SKIPPED, and "본문이 없습니다" would be a wrong explanation --
    // but "생성 중" would be worse, because it never resolves.
    render(<DocumentSummary summary={summary({
      state: 'SKIPPED', reason: 'PROVIDER_DISABLED', content: null, available: false,
    })} />)
    expect(screen.getByText('요약 기능이 현재 비활성화되어 있습니다.')).toBeInTheDocument()
    expect(screen.queryByText(/생성하고 있습니다/)).not.toBeInTheDocument()
  })

  it('separates the three things SKIPPED can mean', () => {
    // One status column, three causes. Collapsing them tells a reader that a
    // large document is empty, which they have no way to detect as wrong.
    const cases = [
      ['NO_TEXT', '이 문서에는 요약할 본문이 없습니다.'],
      ['TOO_LARGE', '문서가 너무 커 현재 요약을 생성할 수 없습니다.'],
      ['PROVIDER_DISABLED', '요약 기능이 현재 비활성화되어 있습니다.'],
    ] as const
    const seen = new Set<string>()
    for (const [reason, message] of cases) {
      const { unmount } = render(
        <DocumentSummary summary={summary({ state: 'SKIPPED', reason, content: null })} />,
      )
      expect(screen.getByText(message)).toBeInTheDocument()
      seen.add(message)
      unmount()
    }
    expect(seen.size).toBe(cases.length)
  })

  it('reports a failure without suggesting the document is unusable', () => {
    render(<DocumentSummary summary={summary({ state: 'FAILED', content: null })} />)
    expect(screen.getByText(/생성하지 못했습니다/)).toBeInTheDocument()
  })

  it('never renders stale text for a state that is not SUCCESS', () => {
    // A RUNNING revision can still hold text from an earlier attempt. The API
    // nulls it, and this asserts the component does not resurrect it either.
    render(<DocumentSummary summary={summary({ state: 'RUNNING' })} />)
    expect(screen.queryByText(/서버 장애 대응 절차를 설명합니다/)).not.toBeInTheDocument()
  })

  it('renders nothing beyond the fields the contract defines', () => {
    // Guards against a future response leaking pipeline details: even if extra
    // keys arrive, the component must not put them on screen. Written without
    // naming any vendor, so the repo-wide scan that keeps provider names out of
    // the frontend still means what it says.
    const withExtras = {
      ...summary(),
      summary_provider: 'PROVIDER-SENTINEL',
      summary_model: 'MODEL-SENTINEL',
      summary_prompt_version: 'PROMPT-SENTINEL',
    } as unknown as Summary
    const { container } = render(<DocumentSummary summary={withExtras} />)
    expect(container.textContent).not.toMatch(/SENTINEL/)
  })
})
