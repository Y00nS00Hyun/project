"""Administrator rename and move of an original file in the shared folder.

The shared folder stays the source of truth. This is the one place the system
changes it, and only because an administrator asked, only with the feature
switched on, and only through a second, separate mount: ingestion, search,
parsing and download keep reading SHARED_ROOT, which stays read-only.

Order of operations, each a precondition for the next:

  1. feature on and write root usable        (else FEATURE_UNAVAILABLE)
  2. document visible, active, not processing (row locked for the duration)
  3. source file really exists under the write root
  4. destination computed with the existing canonical-path helpers, never by
     joining strings, and checked to stay inside the root with no symlink
  5. atomic rename that refuses to replace an existing file
  6. documents row updated with the ingestion scanner's own relocate_document()

The file moves first. If the database step then fails, the file is moved back;
if even that fails, the next scan's 1:1 rename detection relinks the document,
exactly as it would for a rename made directly on the file server.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import stat as stat_module
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from auth import audit
from ingestion.document_type import classify_filename
from ingestion.exceptions import PathOutsideRootError
from ingestion.file_scanner import (
    DiscoveredFile,
    assert_within_root,
    canonical_relative_path,
    resolve_source_path,
)
from ingestion.path_encoding import display_name, from_canonical, to_canonical
from ingestion.repository import ACTIVE_JOB_STATUSES, IngestionRepository
from search.exceptions import InvalidSearchRequestError
from search.folder_paths import normalize_folder_path
from search.repository import READ_ACL_PREDICATE, READ_PERMISSIONS

logger = logging.getLogger("documents.file_management")

FLAG = "DOCUMENT_FILE_MANAGEMENT_ENABLED"
WRITE_ROOT_ENV = "SHARED_WRITE_ROOT"
_TRUE_VALUES = {"1", "true", "yes", "on"}

AT_FDCWD = -100
RENAME_NOREPLACE = 1
MAX_NAME_BYTES = 255


# ---------------------------------------------------------------------------
# Errors -- each maps to one API response in the router
# ---------------------------------------------------------------------------

class RelocationError(Exception):
    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class RelocationUnavailable(RelocationError):
    """Feature off, root unusable, no write permission, or no safe rename."""


class DocumentNotRelocatable(RelocationError):
    """Not visible to the caller, deleted, or absent. Reported as not found."""


class InvalidRelocation(RelocationError):
    """A filename or folder path that is not acceptable."""


class DocumentBusy(RelocationError):
    """A newer revision or a processing job is still in flight."""


class DestinationExists(RelocationError):
    """Something already has the destination name. Never overwritten."""


class SourceFileMissing(RelocationError):
    """The document's file is not where the database says it is."""


class FolderExists(RelocationError):
    """A file or folder already has the requested name. Never merged or replaced."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileManagementConfig:
    enabled: bool
    write_root: Path | None

    @property
    def usable(self) -> bool:
        return self.enabled and self.write_root is not None and self.write_root.is_dir()


def load_config() -> FileManagementConfig:
    """Read from the environment each time; there is nothing worth caching.

    Off unless the flag is explicitly true *and* a write root is configured.
    Setting the flag without the override that mounts the write root leaves it
    off, rather than falling back to the read-only mount.
    """
    enabled = os.environ.get(FLAG, "").strip().lower() in _TRUE_VALUES
    root = os.environ.get(WRITE_ROOT_ENV, "").strip()
    return FileManagementConfig(enabled=enabled, write_root=Path(root) if root else None)


def file_management_available(user_id: str, connection_factory: Callable[[], psycopg.Connection]) -> bool:
    """Whether this caller may use the actions at all: feature usable and admin."""
    if not load_config().usable:
        return False
    from auth.repository import AuthRepository

    return AuthRepository(connection_factory).is_system_admin(user_id)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def split_source_path(source_path: str) -> tuple[str, str]:
    """(folder, filename), both canonical. Folder is "" at the top level."""
    folder, _, name = source_path.rpartition("/")
    return folder, name


def document_location(source_path: str) -> dict[str, Any]:
    """What the UI may show about where a file is: relative and readable only."""
    folder, name = split_source_path(source_path)
    return {
        "file_name": display_name(name),
        "folder_path": folder or None,
        "folder_name": display_name(split_source_path(folder)[1]) if folder else None,
    }


def validate_segment(value: str, what: str) -> str:
    """One path segment: no separator, no NUL or control character, not . or ..

    Shared by file names and folder names, so both refuse exactly the same
    things. ``what`` only words the message ("파일명", "폴더 이름").
    """
    name = value.strip()
    if not name:
        raise InvalidRelocation(f"{what}을 입력해 주세요.")
    if name in (".", "..") or any(ch in name for ch in ("/", "\\", "\x00")):
        raise InvalidRelocation(f"{what}에는 경로 구분자나 사용할 수 없는 문자를 넣을 수 없습니다.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise InvalidRelocation(f"{what}에 제어 문자를 넣을 수 없습니다.")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidRelocation(f"{what}에 사용할 수 없는 문자가 있습니다.") from None
    if len(encoded) > MAX_NAME_BYTES:
        raise InvalidRelocation(f"{what}이 너무 깁니다.")
    return name


def validate_filename(filename: str, file_type: str) -> str:
    """A bare file name with the document's own extension, or InvalidRelocation."""
    name = validate_segment(filename, "파일명")
    expected = f".{file_type.lower()}"
    if Path(name).suffix.lower() != expected or not Path(name).stem.strip():
        raise InvalidRelocation(f"파일 확장자는 {expected}로 유지해야 합니다.")
    return name


def rename_noreplace(source: Path, destination: Path) -> None:
    """Atomic rename that fails with EEXIST instead of replacing the target.

    os.rename and os.replace both overwrite on POSIX, so neither is used. There
    is deliberately no copy-and-delete fallback: a filesystem without
    RENAME_NOREPLACE gets a refusal, not a weaker guarantee.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise OSError(errno.ENOSYS, "renameat2 is not available")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(AT_FDCWD, os.fsencode(str(source)), AT_FDCWD, os.fsencode(str(destination)),
                      RENAME_NOREPLACE)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _translate_os_error(exc: OSError) -> RelocationError:
    if exc.errno == errno.EEXIST:
        return DestinationExists("같은 이름의 파일이 이미 존재합니다.")
    if exc.errno == errno.ENOENT:
        return SourceFileMissing("원본 파일을 찾을 수 없습니다.")
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return RelocationUnavailable("공유폴더에 쓰기 권한이 없습니다.")
    return RelocationUnavailable("이 저장소에서는 안전한 이름 변경/이동을 지원하지 않습니다.")


# ---------------------------------------------------------------------------
# The operation
# ---------------------------------------------------------------------------

_LOCK_SQL = f"""
SELECT d.id, d.source_path, d.file_type,
       d.current_revision_id IS DISTINCT FROM d.latest_revision_id AS latest_pending,
       EXISTS (
           SELECT 1 FROM processing_jobs j
           JOIN document_revisions r ON r.id = j.document_revision_id
           WHERE r.document_id = d.id AND j.status = ANY(%(active)s)
       ) AS has_active_job
FROM documents d
WHERE d.id = %(document_id)s
  AND d.is_deleted = FALSE
  AND {READ_ACL_PREDICATE}
FOR UPDATE OF d
"""


def relocate(
    connection_factory: Callable[[], psycopg.Connection],
    config: FileManagementConfig,
    user_id: str,
    document_id: str,
    *,
    filename: str | None,
    folder_path: str | None,
) -> dict[str, Any]:
    """Rename and/or move one document's file. ``None`` keeps that part as is.

    ``folder_path`` is a canonical path from GET /folders; ``""`` is the top
    level. The caller has already established the user is an administrator.
    """
    if not config.usable or config.write_root is None:
        raise RelocationUnavailable("원본 파일 관리 기능이 꺼져 있습니다.")
    root = config.write_root.resolve()

    conn = connection_factory()
    try:
        conn.autocommit = False
        with conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(_LOCK_SQL, {
                    "document_id": document_id,
                    "user_id": user_id,
                    "read_permissions": list(READ_PERMISSIONS),
                    "active": list(ACTIVE_JOB_STATUSES),
                })
            except psycopg.errors.InvalidTextRepresentation:
                raise DocumentNotRelocatable() from None
            row = cur.fetchone()
        if row is None:
            raise DocumentNotRelocatable()
        if row["latest_pending"] or row["has_active_job"]:
            raise DocumentBusy("이 문서는 현재 처리 중입니다. 처리가 완료된 뒤 다시 시도해 주세요.")

        old_source_path: str = row["source_path"]
        current_folder, current_name = split_source_path(old_source_path)

        # The file as the filesystem names it. The literal path is used for the
        # rename; resolve_source_path is the containment check.
        try:
            resolve_source_path(old_source_path, root)
        except PathOutsideRootError:
            raise SourceFileMissing("원본 파일을 찾을 수 없습니다.") from None
        source = root / from_canonical(old_source_path)
        if source.is_symlink() or not source.is_file():
            raise SourceFileMissing("원본 파일을 찾을 수 없습니다.")

        # Name: unchanged unless a different readable name was asked for. The
        # comparison is against the display form, so a legacy CP949 name that
        # is only being moved keeps its original bytes.
        raw_name = from_canonical(current_name)
        if filename is not None:
            requested = validate_filename(filename, row["file_type"])
            if requested != display_name(current_name):
                raw_name = requested

        # Folder: unchanged unless given; "" is the top level.
        target_folder = current_folder
        if folder_path is not None:
            try:
                target_folder = normalize_folder_path(folder_path) or ""
            except InvalidSearchRequestError:
                raise InvalidRelocation("이동할 폴더 경로가 올바르지 않습니다.") from None

        renamed = raw_name != from_canonical(current_name)
        moved = target_folder != current_folder
        previous = document_location(old_source_path)
        if not renamed and not moved:
            conn.rollback()
            return {"changed": False, "renamed": False, "moved": False,
                    "title": _title_for(old_source_path), "location": previous,
                    "previous_location": previous}

        destination_dir = root / from_canonical(target_folder) if target_folder else root
        try:
            resolved_dir = assert_within_root(destination_dir, root)
        except PathOutsideRootError:
            raise InvalidRelocation("이동할 폴더 경로가 올바르지 않습니다.") from None
        # Equal only if no component is a symlink: the scanner does not follow
        # symlinks, so a file moved behind one would vanish from the index.
        if resolved_dir != destination_dir or not destination_dir.is_dir():
            raise InvalidRelocation("이동할 폴더가 존재하지 않습니다.")

        destination = destination_dir / raw_name
        if os.path.lexists(destination):
            raise DestinationExists("같은 이름의 파일이 이미 존재합니다.")

        try:
            rename_noreplace(source, destination)
        except OSError as exc:
            raise _translate_os_error(exc) from None

        try:
            stat = destination.stat()
            discovered = DiscoveredFile(
                absolute_path=destination,
                relative_path=canonical_relative_path(destination, root),
                filename=raw_name,
                extension=destination.suffix.lower().lstrip("."),
                size=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
            repo = IngestionRepository(conn)
            # The same update the scanner makes when it detects a rename:
            # source_path, title and original_filename only. Revisions, their
            # source_path_at_ingest, pointers and every reference stay put.
            repo.relocate_document(
                document_id,
                source_path=discovered.relative_path,
                title=discovered.title,
                original_filename=discovered.canonical_filename,
            )
            if renamed:
                # The kind is derived from the name, as in the scanner.
                repo.set_document_type(
                    document_id, classify_filename(discovered.display_filename).tag_name,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            try:
                rename_noreplace(destination, source)
            except OSError:
                # Left for the next scan's rename detection. No path in the log.
                logger.error("document.relocate_rollback_failed")
            raise
    finally:
        conn.close()

    new_source_path = discovered.relative_path
    metadata = {"from_path": old_source_path, "to_path": new_source_path}
    if renamed:
        audit.record(connection_factory, audit.DOCUMENT_RENAMED, actor_user_id=user_id,
                     target_id=document_id, target_type="DOCUMENT", metadata=metadata)
    if moved:
        audit.record(connection_factory, audit.DOCUMENT_MOVED, actor_user_id=user_id,
                     target_id=document_id, target_type="DOCUMENT", metadata=metadata)
    # Counts only: a file name can itself be sensitive.
    logger.info("document.relocated", extra={"renamed": renamed, "moved": moved})
    return {
        "changed": True, "renamed": renamed, "moved": moved,
        "title": discovered.title,
        "location": document_location(new_source_path),
        "previous_location": previous,
    }


def _title_for(source_path: str) -> str:
    return Path(display_name(split_source_path(source_path)[1])).stem


# ---------------------------------------------------------------------------
# Directories (administrator only)
# ---------------------------------------------------------------------------

def _directory_entry(canonical: str) -> dict[str, Any]:
    return {
        "path": canonical,
        "name": display_name(split_source_path(canonical)[1]),
        "depth": canonical.count("/") + 1,
    }


def _subdirectories(path: Path) -> list[Path]:
    """Real subdirectories of ``path``, never symlinks, in display-name order."""
    try:
        with os.scandir(path) as entries:
            found = [entry for entry in entries if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []  # an unreadable directory contributes nothing
    found.sort(key=lambda entry: display_name(to_canonical(entry.name)))
    return [Path(entry.path) for entry in found]


def list_directories(config: FileManagementConfig) -> list[dict[str, Any]]:
    """Every real directory under the write root, parents before children.

    Unlike GET /folders this is the filesystem, not the index: an empty folder
    is listed. It exists for administrators choosing where to create a folder
    or move a file, and is never shown to ordinary users -- a directory holding
    only documents they may not read would otherwise disclose its name.

    Symlinks are neither listed nor descended into, which is also what rules out
    cycles; every entry is re-checked to resolve inside the root. Only canonical
    relative paths leave this function.
    """
    if not config.usable or config.write_root is None:
        raise RelocationUnavailable("원본 파일 관리 기능이 꺼져 있습니다.")
    root = config.write_root.resolve()
    result: list[dict[str, Any]] = []
    stack = list(reversed(_subdirectories(root)))
    while stack:
        path = stack.pop()
        try:
            canonical = canonical_relative_path(path, root)
        except PathOutsideRootError:
            continue
        if path.resolve() != path:
            continue
        result.append(_directory_entry(canonical))
        stack.extend(reversed(_subdirectories(path)))
    return result


def _inherit_parent_access(target: Path, parent_stat: os.stat_result) -> None:
    """Give the new folder its parent's group and permission bits.

    The backend runs as uid 10001 with a restrictive umask, so a plain mkdir
    would produce a folder only that uid can write to -- and nobody adding
    documents through the file server could put anything in it. Best effort:
    failing here leaves a valid, if narrower, folder.
    """
    try:
        os.chown(target, -1, parent_stat.st_gid)
    except OSError:
        logger.warning("folder.group_not_inherited")
    try:
        os.chmod(target, stat_module.S_IMODE(parent_stat.st_mode))
    except OSError:
        logger.warning("folder.mode_not_inherited")


def create_directory(
    connection_factory: Callable[[], psycopg.Connection],
    config: FileManagementConfig,
    user_id: str,
    parent_path: str,
    name: str,
) -> dict[str, Any]:
    """Create exactly one folder under an existing folder (``""`` is the top).

    No intermediate folder is ever created, nothing existing is merged or
    replaced, and the parent must be a real directory reached without a
    symlink. The caller has already established the user is an administrator.
    """
    if not config.usable or config.write_root is None:
        raise RelocationUnavailable("원본 파일 관리 기능이 꺼져 있습니다.")
    root = config.write_root.resolve()

    folder_name = validate_segment(name, "폴더 이름")
    try:
        parent = normalize_folder_path(parent_path) or ""
    except InvalidSearchRequestError:
        raise InvalidRelocation("상위 폴더 경로가 올바르지 않습니다.") from None

    parent_dir = root / from_canonical(parent) if parent else root
    try:
        resolved = assert_within_root(parent_dir, root)
    except PathOutsideRootError:
        raise InvalidRelocation("상위 폴더 경로가 올바르지 않습니다.") from None
    if resolved != parent_dir or not parent_dir.is_dir():
        raise InvalidRelocation("상위 폴더가 존재하지 않습니다.")

    target = parent_dir / folder_name
    if os.path.lexists(target):
        raise FolderExists("같은 이름의 파일 또는 폴더가 이미 존재합니다.")
    try:
        parent_stat = parent_dir.stat()
        # One level only: os.mkdir, never makedirs / parents=True. It also
        # fails atomically if something takes the name after the check above.
        os.mkdir(target)
    except FileExistsError:
        raise FolderExists("같은 이름의 파일 또는 폴더가 이미 존재합니다.") from None
    except FileNotFoundError:
        raise InvalidRelocation("상위 폴더가 존재하지 않습니다.") from None
    except OSError as exc:
        raise _translate_os_error(exc) from None
    _inherit_parent_access(target, parent_stat)

    canonical = canonical_relative_path(target, root)
    audit.record(connection_factory, audit.FOLDER_CREATED, actor_user_id=user_id,
                 target_type="FOLDER", metadata={"path": canonical})
    # No name in the log line: a folder name can itself be sensitive.
    logger.info("folder.created", extra={"depth": canonical.count("/") + 1})
    return _directory_entry(canonical)
