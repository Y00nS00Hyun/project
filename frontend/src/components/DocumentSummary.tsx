import type { DocumentSummary as Summary } from '../api/types'
import { formatDate } from '../labels'

/**
 * The document's precomputed overview.
 *
 * Read-only, and never a trigger: opening a document does not start
 * generation, so this component has no retry button and makes no request. What
 * it shows is whatever the last background run left behind.
 *
 * Every state gets its own sentence. The one that matters is the difference
 * between "being written" and "not written here": telling a reader a summary
 * is on its way in a deployment that generates none is the failure this
 * component exists to avoid.
 */
export function DocumentSummary({ summary }: { summary: Summary }) {
  return (
    <section className="section" aria-labelledby="document-summary-heading">
      <h2 className="section-title" id="document-summary-heading">
        문서 요약
      </h2>
      <Body summary={summary} />
    </section>
  )
}

function Body({ summary }: { summary: Summary }) {
  if (summary.state === 'SUCCESS' && summary.content) {
    return (
      <>
        <p className="summary-text">{summary.content}</p>
        <p className="state-hint">
          현재 검색 버전을 기준으로 자동 생성된 요약입니다
          {summary.generated_at ? ` · ${formatDate(summary.generated_at)}` : ''}. 정확한 내용은
          원본 문서를 확인해 주세요.
        </p>
      </>
    )
  }

  // Checked before the per-state messages: when generation is off, "생성 중"
  // would be a promise nothing in this deployment can keep.
  if (!summary.available) {
    return <p className="state-hint">이 환경에서는 문서 요약을 생성하지 않습니다.</p>
  }

  switch (summary.state) {
    case 'PENDING':
    case 'RUNNING':
      return <p className="state-hint">요약을 생성하고 있습니다. 잠시 후 다시 확인해 주세요.</p>
    case 'SKIPPED':
      return <p className="state-hint">이 문서에는 요약할 본문이 없습니다.</p>
    default:
      // FAILED, or SUCCESS with no stored text. Both mean the same thing to a
      // reader, and neither says anything about the document being usable --
      // search and download are unaffected.
      return <p className="state-hint">요약을 생성하지 못했습니다. 원본 문서를 확인해 주세요.</p>
  }
}
