import { describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FolderTree } from './FolderTree'
import { ancestorPaths, buildTree, type FolderNode } from '../api/folders'
import { FOLDERS, renderAt } from '../test/helpers'

const LEGACY_PATH = '%C7%C1%B7%CE%C1%A7Ʈ_B'

function render(
  selected: string | null = null,
  folders: FolderNode[] = FOLDERS,
  totalDocuments = 9,
  extra: { topLevelDocuments?: number; topLevelSelected?: boolean } = {},
) {
  const onSelect = vi.fn()
  const onSelectTopLevel = vi.fn()
  const result = renderAt(
    <FolderTree
      folders={folders}
      totalDocuments={totalDocuments}
      topLevelDocuments={extra.topLevelDocuments ?? 0}
      topLevelSelected={extra.topLevelSelected ?? false}
      selected={selected}
      onSelect={onSelect}
      onSelectTopLevel={onSelectTopLevel}
    />,
  )
  return { ...result, onSelect, onSelectTopLevel }
}

describe('buildTree', () => {
  it('nests children under their parent_path', () => {
    const tree = buildTree(FOLDERS)
    const projectA = tree.find((n) => n.path === '프로젝트_A')!
    expect(projectA.children.map((c) => c.name).sort()).toEqual(['complete', 'requirements'].map(
      (_, i) => ['완료', '요구사항'][i],
    ))
  })

  it('keeps a node whose parent is absent at the root', () => {
    // The parent can only be missing because it held nothing readable; hiding
    // the child too would lose a folder the user is allowed to see.
    const orphan: FolderNode = {
      path: 'a/b', name: 'b', parent_path: 'a', depth: 2, document_count: 1,
    }
    expect(buildTree([orphan]).map((n) => n.path)).toEqual(['a/b'])
  })

  it('nests by path, never by name', () => {
    const tree = buildTree(FOLDERS)
    expect(tree.find((n) => n.path === LEGACY_PATH)).toBeDefined()
    expect(tree.find((n) => n.path === '프로젝트_B')).toBeUndefined()
  })
})

describe('ancestorPaths', () => {
  it('walks up using canonical paths', () => {
    expect(ancestorPaths(FOLDERS, '프로젝트_A/요구사항')).toEqual(['프로젝트_A'])
    expect(ancestorPaths(FOLDERS, '프로젝트_A')).toEqual([])
  })
})

describe('FolderTree', () => {
  it('shows display names, never the canonical path', () => {
    const { container } = render()
    expect(screen.getByText('프로젝트_B')).toBeInTheDocument()
    // The escaped path is an implementation detail and must not reach the screen.
    expect(container.textContent).not.toContain('%C7%C1')
  })

  it('starts collapsed and expands on demand', async () => {
    render()
    expect(screen.queryByText('요구사항')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /프로젝트_A 펼치기/ }))
    expect(screen.getByText('요구사항')).toBeInTheDocument()
  })

  it('collapses again', async () => {
    render()
    await userEvent.click(screen.getByRole('button', { name: /프로젝트_A 펼치기/ }))
    await userEvent.click(screen.getByRole('button', { name: /프로젝트_A 접기/ }))
    expect(screen.queryByText('요구사항')).not.toBeInTheDocument()
  })

  it('reports the canonical path on selection, not the name', async () => {
    const { onSelect } = render()
    // Exact accessible name: the toggle button also mentions the folder.
    await userEvent.click(screen.getByRole('button', { name: '프로젝트_B, 문서 2건' }))
    expect(onSelect).toHaveBeenCalledWith(LEGACY_PATH)
  })

  it('clicking the selected folder clears it', async () => {
    const { onSelect } = render('프로젝트_A')
    await userEvent.click(screen.getByRole('button', { name: '프로젝트_A, 문서 3건' }))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('clears the selection through the root row', async () => {
    const { onSelect } = render('프로젝트_A')
    // The root row is the clear control: selecting it means "no folder
    // filter", which is what clearing is.
    await userEvent.click(screen.getByRole('button', { name: /전체 문서, 문서 9건/ }))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('marks the root as selected when no folder is', () => {
    render(null)
    const root = screen.getAllByRole('treeitem')[0]
    expect(root.getAttribute('aria-selected')).toBe('true')
    expect(within(root).getByText('전체 문서')).toBeInTheDocument()
  })

  it('does not name the shared folder after a directory on the server', () => {
    // The root stands for the shared folder, and the server's mount path never
    // reaches a client -- its last segment would be a piece of that path.
    const { container } = render(null)
    expect(container.textContent).not.toMatch(/data|shared|opt|docsearch/i)
  })

  it('marks the selected folder for assistive technology', () => {
    render('프로젝트_A')
    const selected = screen.getAllByRole('treeitem').find(
      (node) => node.getAttribute('aria-selected') === 'true',
    )
    expect(within(selected!).getByText('프로젝트_A')).toBeInTheDocument()
  })

  it('expands the branch leading to a selection restored from the URL', () => {
    // A shared link or a reload must show the selected folder, not a collapsed
    // root the user has to dig through again.
    render('프로젝트_A/요구사항')
    expect(screen.getByText('요구사항')).toBeInTheDocument()
  })

  it('shows the subtree document count', () => {
    render()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('still shows the root when the shared folder has no subfolders', () => {
    // The corpus this was built for has every document at the top level. An
    // empty panel reads as a failure; the root row says "there are documents,
    // just no folders".
    render(null, [], 7)
    const root = screen.getAllByRole('treeitem')[0]
    expect(within(root).getByText('전체 문서')).toBeInTheDocument()
    expect(within(root).getByText('7')).toBeInTheDocument()
    expect(screen.queryByText('표시할 폴더가 없습니다.')).not.toBeInTheDocument()
  })

  it('nests the top-level folders under the root', () => {
    render()
    const groups = screen.getAllByRole('group')
    expect(within(groups[0]).getByText('프로젝트_A')).toBeInTheDocument()
  })

  it('renders only folders the server returned', () => {
    // ACL happens on the server; there is nothing to hide here.
    render()
    expect(screen.queryByText('비공개_프로젝트')).not.toBeInTheDocument()
  })
})

describe('the root row and its chevron do different things', () => {
  // Two controls on one row, and they must not be confused for each other.
  // The label answers "show me everything"; the chevron answers "hide the
  // folder list". Wiring either to the other's job makes one of them
  // unreachable.
  const ROOT_LABEL = /^전체 문서, 문서 \d+건$/
  const CHEVRON = /^전체 문서 (펼치기|접기)$/

  it('clicking the label clears the folder filter', async () => {
    const { onSelect } = render('프로젝트_A')
    await userEvent.click(screen.getByRole('button', { name: ROOT_LABEL }))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('clicking the label while nothing is selected asks for nothing new', async () => {
    const { onSelect } = render(null)
    await userEvent.click(screen.getByRole('button', { name: ROOT_LABEL }))
    // Still null: selecting the root *is* the absence of a filter, so the page
    // is told the same thing it already knew rather than a new folder value.
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('clicking the chevron collapses and expands only the folder list', async () => {
    render(null)
    expect(screen.getByText('프로젝트_A')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: CHEVRON }))
    expect(screen.queryByText('프로젝트_A')).not.toBeInTheDocument()
    // The root row itself stays -- collapsing hides children, not the tree.
    expect(screen.getByRole('button', { name: ROOT_LABEL })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: CHEVRON }))
    expect(screen.getByText('프로젝트_A')).toBeInTheDocument()
  })

  it('the chevron never changes the selection', async () => {
    const { onSelect } = render('프로젝트_A')
    await userEvent.click(screen.getByRole('button', { name: CHEVRON }))
    await userEvent.click(screen.getByRole('button', { name: CHEVRON }))
    // Two clicks, no selection change at all -- not even a redundant one,
    // which would refetch the results for a purely visual action.
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('a collapsed root still reports the total', async () => {
    render(null, FOLDERS, 9)
    await userEvent.click(screen.getByRole('button', { name: CHEVRON }))
    expect(screen.getByRole('button', { name: '전체 문서, 문서 9건' })).toBeInTheDocument()
  })

  it('has no chevron when there are no subfolders', () => {
    render(null, [], 7)
    expect(screen.queryByRole('button', { name: CHEVRON })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: ROOT_LABEL })).toBeInTheDocument()
  })

  it('offers no separate clear-selection control', () => {
    render('프로젝트_A')
    // The root row is that control. A second one beside it would leave two
    // ways to say the same thing and a question about which is canonical.
    expect(screen.queryByRole('button', { name: '전체 문서 보기' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: ROOT_LABEL })).toHaveLength(1)
  })

  it('reveals a selection made while the root was collapsed', async () => {
    // Otherwise the sidebar shows one collapsed row while the results are
    // filtered by a folder the user cannot see.
    const { rerender } = render(null)
    await userEvent.click(screen.getByRole('button', { name: CHEVRON }))
    expect(screen.queryByText('프로젝트_A')).not.toBeInTheDocument()

    rerender(
      <FolderTree
        folders={FOLDERS}
        totalDocuments={9}
        selected="프로젝트_A/요구사항"
        onSelect={vi.fn()}
      />,
    )
    expect(screen.getByText('요구사항')).toBeInTheDocument()
  })
})

describe('subfolders filter, and say so', () => {
  it('clicking a subfolder sends its canonical path', async () => {
    const { onSelect } = render(null)
    await userEvent.click(screen.getByRole('button', { name: /^프로젝트_A, 문서 3건$/ }))
    expect(onSelect).toHaveBeenCalledWith('프로젝트_A')
  })

  it('sends the canonical path even when it differs from the name', async () => {
    const { onSelect } = render(null)
    // The legacy folder reads 프로젝트_B and is stored as escaped bytes. A path
    // rebuilt from the name would match no document.
    await userEvent.click(screen.getByRole('button', { name: /^프로젝트_B, 문서 2건$/ }))
    expect(onSelect).toHaveBeenCalledWith(LEGACY_PATH)
  })

  it('clicking the selected subfolder clears back to everything', async () => {
    const { onSelect } = render('프로젝트_A')
    await userEvent.click(screen.getByRole('button', { name: /^프로젝트_A, 문서 3건$/ }))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('a subfolder chevron does not change the selection either', async () => {
    const { onSelect } = render(null)
    await userEvent.click(screen.getByRole('button', { name: '프로젝트_A 펼치기' }))
    expect(screen.getByText('요구사항')).toBeInTheDocument()
    expect(onSelect).not.toHaveBeenCalled()
  })
})

describe('documents in no folder get a row of their own', () => {
  // Without it the folder counts add up to less than the total and the
  // difference belongs to nothing on screen -- which is most of the corpus in
  // a shared folder whose files sit at the top.
  const OTHER = /^기타, 문서 \d+건$/

  it('appears beside the folders with its own count', () => {
    render(null, FOLDERS, 12, { topLevelDocuments: 7 })
    const row = screen.getByRole('button', { name: '기타, 문서 7건' })
    expect(row).toBeInTheDocument()
    // A sibling of the real folders, not a child of one.
    expect(within(screen.getAllByRole('group')[0]).getByText('기타')).toBeInTheDocument()
  })

  it('selecting it asks for the top-level filter, not a folder path', async () => {
    const { onSelect, onSelectTopLevel } = render(null, FOLDERS, 12, {
      topLevelDocuments: 7,
    })
    await userEvent.click(screen.getByRole('button', { name: OTHER }))
    expect(onSelectTopLevel).toHaveBeenCalled()
    // Never a path: every path starts at the root, so no prefix selects
    // exactly the documents that are not under one.
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('clicking it again clears back to everything', async () => {
    const { onSelect, onSelectTopLevel } = render(null, FOLDERS, 12, {
      topLevelDocuments: 7, topLevelSelected: true,
    })
    await userEvent.click(screen.getByRole('button', { name: OTHER }))
    expect(onSelect).toHaveBeenCalledWith(null)
    expect(onSelectTopLevel).not.toHaveBeenCalled()
  })

  it('is marked selected, and the root is not, while it is active', () => {
    render(null, FOLDERS, 12, { topLevelDocuments: 7, topLevelSelected: true })
    const selected = screen.getAllByRole('treeitem').filter(
      (node) => node.getAttribute('aria-selected') === 'true',
    )
    expect(selected).toHaveLength(1)
    expect(within(selected[0]).getByText('기타')).toBeInTheDocument()
  })

  it('is absent when every document is already in a folder', () => {
    render(null, FOLDERS, 5, { topLevelDocuments: 0 })
    expect(screen.queryByText('기타')).not.toBeInTheDocument()
  })

  it('is absent when there are no folders to be outside of', () => {
    // Everything is at the top, so 기타 would repeat what 전체 문서 says.
    render(null, [], 7, { topLevelDocuments: 7 })
    expect(screen.queryByText('기타')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '전체 문서, 문서 7건' })).toBeInTheDocument()
  })

  it('hides with the rest when the root is collapsed', async () => {
    render(null, FOLDERS, 12, { topLevelDocuments: 7 })
    await userEvent.click(screen.getByRole('button', { name: /^전체 문서 (펼치기|접기)$/ }))
    expect(screen.queryByText('기타')).not.toBeInTheDocument()
  })

  it('has no chevron of its own', () => {
    render(null, FOLDERS, 12, { topLevelDocuments: 7 })
    expect(screen.queryByRole('button', { name: /^기타 (펼치기|접기)$/ })).not.toBeInTheDocument()
  })
})
