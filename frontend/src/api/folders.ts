import { getJson, type RequestOptions } from './client'

export interface FolderNode {
  /**
   * Canonical relative path -- the folder's identity, and what goes back to
   * the server as `folder_path`.
   *
   * For a folder created outside UTF-8 this is escaped and unreadable; that is
   * expected. Never build one by joining `name`s: the result would match no
   * document. Keep the value the server gave you and send it back verbatim.
   */
  path: string
  /** The last segment, rendered for reading. Display only. */
  name: string
  parent_path: string | null
  depth: number
  /** Readable documents anywhere beneath this folder. */
  document_count: number
}

export interface FolderListResponse {
  items: FolderNode[]
  /**
   * Every document the caller may browse, in a folder or not.
   *
   * Not the sum of the top-level `document_count` values: a document sitting
   * at the top of the shared folder belongs to no folder and appears in no
   * item, so summing would under-report -- and a shared folder with no
   * subdirectories would report zero while holding documents.
   */
  total_documents: number
  /**
   * Documents belonging to no folder -- the ones at the top of the shared
   * folder. They appear in no `items` entry, so without this the sidebar can
   * show the folders and the total but not account for the difference.
   *
   * Selected with `top_level_only`, not a `folder_path`: every path starts at
   * the root, so no prefix picks out exactly the documents not under one.
   */
  top_level_documents: number
}

/**
 * The shared folder's structure, as far as the caller may see it.
 *
 * Takes no filters on purpose: the tree is navigation, so it stays stable
 * while year, document kind and file type change. A folder holding only
 * manuals does not disappear when the user filters for reports -- selecting it
 * simply returns nothing.
 */
export function fetchFolders(options: RequestOptions = {}): Promise<FolderListResponse> {
  return getJson<FolderListResponse>('/folders', options)
}

export interface FolderTreeNode extends FolderNode {
  children: FolderTreeNode[]
}

/**
 * Assemble the flat list into a tree using `parent_path`.
 *
 * A node whose parent is missing is attached at the root rather than dropped:
 * the parent can only be absent if it held nothing readable, and hiding the
 * child as well would lose a folder the user is allowed to see.
 */
export function buildTree(items: FolderNode[]): FolderTreeNode[] {
  const byPath = new Map<string, FolderTreeNode>()
  for (const item of items) byPath.set(item.path, { ...item, children: [] })

  const roots: FolderTreeNode[] = []
  for (const node of byPath.values()) {
    const parent = node.parent_path == null ? undefined : byPath.get(node.parent_path)
    if (parent) parent.children.push(node)
    else roots.push(node)
  }

  const sortNodes = (nodes: FolderTreeNode[]): void => {
    nodes.sort((a, b) => a.name.localeCompare(b.name, 'ko'))
    for (const node of nodes) sortNodes(node.children)
  }
  sortNodes(roots)
  return roots
}

/** Every ancestor path of `path`, nearest last -- used to auto-expand a selection. */
export function ancestorPaths(items: FolderNode[], path: string): string[] {
  const byPath = new Map(items.map((item) => [item.path, item]))
  const chain: string[] = []
  let current = byPath.get(path)?.parent_path ?? null
  while (current) {
    chain.unshift(current)
    current = byPath.get(current)?.parent_path ?? null
  }
  return chain
}
