import { useCallback, useEffect, useId, useState } from 'react'
import { ApiClientError } from '../api/client'
import { relocateDocument } from '../api/documents'
import { fetchAdminDirectories } from '../api/fileManagement'
import type { AdminDirectory, DocumentLocation, RelocateResponse } from '../api/types'
import { ErrorView } from './StateViews'

export const TOP_LEVEL_FOLDER_LABEL = '최상위 (폴더 없음)'

type Dialog = 'rename' | 'move' | null

/**
 * Rename or move the original file. Rendered only for an administrator while
 * the feature is on; the server enforces both again on every request.
 *
 * Destinations are real directories from GET /admin/directories -- nothing is
 * typed as a path, and no folder is created here.
 */
export function DocumentFileActions({ documentId, location, onRelocated }: {
  documentId: string
  location: DocumentLocation
  onRelocated: (result: RelocateResponse) => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [dialog, setDialog] = useState<Dialog>(null)

  const open = (which: Dialog) => {
    setMenuOpen(false)
    setDialog(which)
  }
  const close = () => setDialog(null)
  const done = (result: RelocateResponse) => {
    setDialog(null)
    onRelocated(result)
  }

  return (
    <div className="file-actions">
      <button type="button" className="button" aria-haspopup="menu" aria-expanded={menuOpen}
              onClick={() => setMenuOpen((value) => !value)}>
        문서 관리 <span aria-hidden="true">▾</span>
      </button>
      {menuOpen && (
        <ul className="file-actions-menu" role="menu">
          <li role="none">
            <button type="button" role="menuitem" onClick={() => open('rename')}>이름 변경</button>
          </li>
          <li role="none">
            <button type="button" role="menuitem" onClick={() => open('move')}>폴더 이동</button>
          </li>
        </ul>
      )}
      {dialog === 'rename' && (
        <RenameDialog documentId={documentId} location={location} onCancel={close} onDone={done} />
      )}
      {dialog === 'move' && (
        <MoveDialog documentId={documentId} location={location} onCancel={close} onDone={done} />
      )}
    </div>
  )
}

function useSubmit(documentId: string, onDone: (result: RelocateResponse) => void) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<ApiClientError | null>(null)
  const submit = useCallback(async (body: Parameters<typeof relocateDocument>[1]) => {
    setSubmitting(true)
    setError(null)
    try {
      onDone(await relocateDocument(documentId, body))
    } catch (caught) {
      setError(caught instanceof ApiClientError
        ? caught
        : new ApiClientError('INTERNAL_ERROR', '요청을 처리하지 못했습니다.', 0, null))
    } finally {
      setSubmitting(false)
    }
  }, [documentId, onDone])
  return { submitting, error, submit }
}

export function DialogFrame({ title, children }: { title: string; children: React.ReactNode }) {
  const titleId = useId()
  return (
    <div className="dialog-backdrop">
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 className="dialog-title" id={titleId}>{title}</h2>
        {children}
      </div>
    </div>
  )
}

function RenameDialog({ documentId, location, onCancel, onDone }: {
  documentId: string
  location: DocumentLocation
  onCancel: () => void
  onDone: (result: RelocateResponse) => void
}) {
  const [name, setName] = useState(location.file_name)
  const { submitting, error, submit } = useSubmit(documentId, onDone)
  const inputId = useId()
  return (
    <DialogFrame title="파일명 변경">
      <form onSubmit={(event) => { event.preventDefault(); void submit({ filename: name }) }}>
        <p className="dialog-label">현재 파일명</p>
        <p className="dialog-value">{location.file_name}</p>
        <label className="dialog-label" htmlFor={inputId}>새 파일명</label>
        <input id={inputId} className="dialog-input" value={name} autoFocus
               onChange={(event) => setName(event.target.value)} />
        {error && <ErrorView error={error} />}
        <div className="dialog-buttons">
          <button type="button" className="button" onClick={onCancel} disabled={submitting}>취소</button>
          <button type="submit" className="button button-primary"
                  disabled={submitting || !name.trim()}>
            {submitting ? '변경 중...' : '변경'}
          </button>
        </div>
      </form>
    </DialogFrame>
  )
}

function MoveDialog({ documentId, location, onCancel, onDone }: {
  documentId: string
  location: DocumentLocation
  onCancel: () => void
  onDone: (result: RelocateResponse) => void
}) {
  // Real directories, empty ones included, so a new empty folder is a valid
  // destination. The sidebar's GET /folders lists only folders with documents.
  const [folders, setFolders] = useState<AdminDirectory[] | null>(null)
  const [loadError, setLoadError] = useState<ApiClientError | null>(null)
  const [target, setTarget] = useState(location.folder_path ?? '')
  const { submitting, error, submit } = useSubmit(documentId, onDone)
  const selectId = useId()

  useEffect(() => {
    const controller = new AbortController()
    fetchAdminDirectories({ signal: controller.signal })
      .then((response) => setFolders(response.directories))
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return
        setLoadError(caught instanceof ApiClientError
          ? caught
          : new ApiClientError('INTERNAL_ERROR', '폴더 목록을 불러오지 못했습니다.', 0, null))
      })
    return () => controller.abort()
  }, [])

  return (
    <DialogFrame title="폴더 이동">
      <form onSubmit={(event) => { event.preventDefault(); void submit({ folder_path: target }) }}>
        <p className="dialog-label">현재 위치</p>
        <p className="dialog-value">{location.folder_name ?? TOP_LEVEL_FOLDER_LABEL}</p>
        <label className="dialog-label" htmlFor={selectId}>이동 위치</label>
        <select id={selectId} className="dialog-input" value={target}
                disabled={folders == null} onChange={(event) => setTarget(event.target.value)}>
          <option value="">{TOP_LEVEL_FOLDER_LABEL}</option>
          {(folders ?? []).map((folder) => (
            <option key={folder.path} value={folder.path}>
              {'\u00a0\u00a0'.repeat(Math.max(0, folder.depth - 1))}{folder.name}
            </option>
          ))}
        </select>
        {loadError && <ErrorView error={loadError} />}
        {error && <ErrorView error={error} />}
        <div className="dialog-buttons">
          <button type="button" className="button" onClick={onCancel} disabled={submitting}>취소</button>
          <button type="submit" className="button button-primary" disabled={submitting || folders == null}>
            {submitting ? '이동 중...' : '이동'}
          </button>
        </div>
      </form>
    </DialogFrame>
  )
}
