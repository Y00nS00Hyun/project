import { useEffect, useMemo, useState } from 'react'
import {
  ancestorPaths,
  buildTree,
  type FolderNode,
  type FolderTreeNode,
} from '../api/folders'

interface Props {
  folders: FolderNode[]
  loading?: boolean
  /** Canonical path of the selected folder, or null. */
  selected: string | null
  onSelect: (path: string | null) => void
}

/**
 * Explorer-style navigation over the shared folder.
 *
 * The tree is built from folders the server was willing to name, so there is
 * nothing to hide here -- a project the user cannot read never arrives.
 *
 * `path` is carried through untouched and handed back on selection. It is
 * never derived from `name`: for a folder created outside UTF-8 the two differ
 * completely, and a path rebuilt from display names matches no document.
 */
export function FolderTree({ folders, loading, selected, onSelect }: Props) {
  const tree = useMemo(() => buildTree(folders), [folders])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // Open the branch leading to the selection, so a shared link or a reload
  // shows the selected folder rather than a collapsed root.
  useEffect(() => {
    if (!selected) return
    setExpanded((current) => {
      const next = new Set(current)
      for (const path of ancestorPaths(folders, selected)) next.add(path)
      next.add(selected)
      return next
    })
  }, [selected, folders])

  const toggle = (path: string) =>
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  if (loading && folders.length === 0) {
    return <p className="folder-empty">폴더를 불러오는 중...</p>
  }
  if (folders.length === 0) {
    return <p className="folder-empty">표시할 폴더가 없습니다.</p>
  }

  return (
    <>
      {selected && (
        <button type="button" className="folder-clear" onClick={() => onSelect(null)}>
          전체 문서 보기
        </button>
      )}
      <ul className="folder-tree" role="tree" aria-label="공유폴더">
        {tree.map((node) => (
          <FolderBranch
            key={node.path}
            node={node}
            expanded={expanded}
            selected={selected}
            onToggle={toggle}
            onSelect={onSelect}
          />
        ))}
      </ul>
    </>
  )
}

interface BranchProps {
  node: FolderTreeNode
  expanded: Set<string>
  selected: string | null
  onToggle: (path: string) => void
  onSelect: (path: string | null) => void
}

function FolderBranch({ node, expanded, selected, onToggle, onSelect }: BranchProps) {
  const hasChildren = node.children.length > 0
  const isOpen = expanded.has(node.path)
  const isSelected = selected === node.path

  return (
    <li role="none">
      <div
        role="treeitem"
        aria-selected={isSelected}
        aria-expanded={hasChildren ? isOpen : undefined}
        className={isSelected ? 'folder-row is-selected' : 'folder-row'}
        style={{ paddingLeft: `${(node.depth - 1) * 14 + 4}px` }}
      >
        {hasChildren ? (
          <button
            type="button"
            className="folder-toggle"
            // The row's own label already names the folder; this control only
            // opens it, and a screen reader reads the state from aria-expanded.
            aria-label={`${node.name} ${isOpen ? '접기' : '펼치기'}`}
            onClick={() => onToggle(node.path)}
          >
            <span aria-hidden="true">{isOpen ? '▾' : '▸'}</span>
          </button>
        ) : (
          <span className="folder-toggle folder-toggle-empty" aria-hidden="true" />
        )}

        <button
          type="button"
          className="folder-name"
          // Spelled out rather than left to the default: concatenating the
          // name and the count reads as "프로젝트_A3" to a screen reader.
          aria-label={`${node.name}, 문서 ${node.document_count}건`}
          // Clicking a selected folder clears it, so a selection is always
          // reversible without hunting for a separate control.
          onClick={() => onSelect(isSelected ? null : node.path)}
        >
          <span className="folder-icon" aria-hidden="true">
            {hasChildren && isOpen ? '📂' : '📁'}
          </span>
          {node.name}
          <span className="folder-count">{node.document_count}</span>
        </button>
      </div>

      {hasChildren && isOpen && (
        <ul role="group">
          {node.children.map((child) => (
            <FolderBranch
              key={child.path}
              node={child}
              expanded={expanded}
              selected={selected}
              onToggle={onToggle}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </li>
  )
}
