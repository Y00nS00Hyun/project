import { useEffect, useId, useState, type FormEvent } from 'react'
import { ApiClientError } from '../api/client'
import { createAdminDirectory, fetchAdminDirectories } from '../api/fileManagement'
import type { AdminDirectory } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { DialogFrame } from './DocumentFileActions'
import { ErrorView } from './StateViews'

export const FOLDER_CREATED_NOTICE =
  '폴더가 생성되었습니다. 문서가 추가되면 문서 검색 목록에 표시됩니다.'
const TOP_LEVEL = '최상위 (공유폴더 루트)'

/**
 * "+ 새 폴더" for administrators, beside the folder sidebar.
 *
 * Shown only when the caller is a system administrator *and* the directory list
 * loads -- which it does only while file management is on. Anyone else sees
 * nothing and triggers no request.
 *
 * The sidebar tree is not touched after creating a folder: it lists folders
 * that hold readable documents, and a new folder holds none yet.
 */
export function CreateFolderAction() {
  const { user } = useAuth()
  const isAdmin = Boolean(user?.is_system_admin)
  const [directories, setDirectories] = useState<AdminDirectory[] | null>(null)
  const [version, setVersion] = useState(0)
  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!isAdmin) return
    const controller = new AbortController()
    fetchAdminDirectories({ signal: controller.signal })
      .then((response) => setDirectories(response.directories))
      .catch(() => {
        // 503 while the feature is off: the action simply is not offered.
        if (!controller.signal.aborted) setDirectories(null)
      })
    return () => controller.abort()
  }, [isAdmin, version])

  if (!isAdmin || directories == null) return null

  return (
    <div className="create-folder">
      <button type="button" className="button" onClick={() => { setNotice(null); setOpen(true) }}>
        + 새 폴더
      </button>
      {notice && <p className="state-hint create-folder-notice" role="status">{notice}</p>}
      {open && (
        <CreateFolderDialog
          directories={directories}
          onCancel={() => setOpen(false)}
          onCreated={() => {
            setOpen(false)
            setNotice(FOLDER_CREATED_NOTICE)
            setVersion((value) => value + 1)
          }}
        />
      )}
    </div>
  )
}

function CreateFolderDialog({ directories, onCancel, onCreated }: {
  directories: AdminDirectory[]
  onCancel: () => void
  onCreated: () => void
}) {
  const [parent, setParent] = useState('')
  const [name, setName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<ApiClientError | null>(null)
  const parentId = useId()
  const nameId = useId()

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await createAdminDirectory({ parent_path: parent, name })
      onCreated()
    } catch (caught) {
      setError(caught instanceof ApiClientError
        ? caught
        : new ApiClientError('INTERNAL_ERROR', '폴더를 만들지 못했습니다.', 0, null))
      setSubmitting(false)
    }
  }

  return (
    <DialogFrame title="새 폴더 만들기">
      <form onSubmit={(event) => void submit(event)}>
        <label className="dialog-label" htmlFor={parentId}>위치</label>
        <select id={parentId} className="dialog-input" value={parent}
                onChange={(event) => setParent(event.target.value)}>
          <option value="">{TOP_LEVEL}</option>
          {directories.map((directory) => (
            <option key={directory.path} value={directory.path}>
              {'\u00a0\u00a0'.repeat(Math.max(0, directory.depth - 1))}{directory.name}
            </option>
          ))}
        </select>
        <label className="dialog-label" htmlFor={nameId}>폴더 이름</label>
        <input id={nameId} className="dialog-input" value={name} autoFocus
               onChange={(event) => setName(event.target.value)} />
        {error && <ErrorView error={error} />}
        <div className="dialog-buttons">
          <button type="button" className="button" onClick={onCancel} disabled={submitting}>취소</button>
          <button type="submit" className="button button-primary" disabled={submitting || !name.trim()}>
            {submitting ? '만드는 중...' : '만들기'}
          </button>
        </div>
      </form>
    </DialogFrame>
  )
}
