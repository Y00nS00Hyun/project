import { describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FolderTree } from './FolderTree'
import { ancestorPaths, buildTree, type FolderNode } from '../api/folders'
import { FOLDERS, renderAt } from '../test/helpers'

const LEGACY_PATH = '%C7%C1%B7%CE%C1%A7Ʈ_B'

function render(selected: string | null = null, folders: FolderNode[] = FOLDERS) {
  const onSelect = vi.fn()
  const result = renderAt(
    <FolderTree folders={folders} selected={selected} onSelect={onSelect} />,
  )
  return { ...result, onSelect }
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

  it('offers an explicit clear control while something is selected', async () => {
    const { onSelect } = render('프로젝트_A')
    await userEvent.click(screen.getByRole('button', { name: '전체 문서 보기' }))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('has no clear control when nothing is selected', () => {
    render(null)
    expect(screen.queryByRole('button', { name: '전체 문서 보기' })).not.toBeInTheDocument()
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

  it('says so when there is nothing to show', () => {
    render(null, [])
    expect(screen.getByText('표시할 폴더가 없습니다.')).toBeInTheDocument()
  })

  it('renders only folders the server returned', () => {
    // ACL happens on the server; there is nothing to hide here.
    render()
    expect(screen.queryByText('비공개_프로젝트')).not.toBeInTheDocument()
  })
})
