import { Link } from 'react-router-dom'
import type { ChatSource } from '../api/types'
import { anchorLabel, fileTypeLabel } from '../labels'

/**
 * Citations under an answer.
 *
 * The union is walked by its `accessible` discriminator, so the compiler --
 * not a convention -- is what stops title, file type or anchor being read on a
 * source the server chose to strip. There is no fallback branch that fills
 * those in from anywhere else.
 */
export function ChatSources({ sources }: { sources: ChatSource[] }) {
  if (sources.length === 0) return null
  return (
    <div className="chat-sources">
      <h3 className="chat-sources-title">출처</h3>
      <ol className="chat-source-list">
        {sources.map((source, index) => (
          <li className="chat-source" key={source.chunk_id}>
            <span className="chat-source-index">출처 {index + 1}</span>
            {source.accessible ? (
              <AccessibleBody source={source} />
            ) : (
              // No title, no position, no file type: the server sent none of
              // them, and guessing from a previous render would leak exactly
              // what the permission change was meant to hide.
              <span className="chat-source-hidden">현재 접근할 수 없는 출처</span>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

function AccessibleBody({ source }: { source: Extract<ChatSource, { accessible: true }> }) {
  const position = anchorLabel(source.anchor)
  return (
    <>
      {/* The contract gives document_id on accessible sources, so the existing
          detail screen can be linked directly. */}
      <Link className="chat-source-title" to={`/documents/${source.document_id}`}>
        {source.title}
      </Link>
      <span className="chat-source-meta">
        <span>{fileTypeLabel(source.file_type)}</span>
        {source.section_title && <span>{source.section_title}</span>}
        {/* null for HWP/HWPX, which carry no page number. Nothing is invented. */}
        {position && <span>{position}</span>}
      </span>
    </>
  )
}
