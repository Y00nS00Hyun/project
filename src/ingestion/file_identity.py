"""File fingerprinting.

Content identity is SHA-256 of the file bytes, matching
``document_revisions.content_hash``. The hash is deliberately NOT unique in the
schema: the same bytes may legitimately exist at several shared-folder paths as
separate logical documents.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .exceptions import FileUnstableError, FileVanishedError

#: Read size for hashing. Large enough to be efficient, small enough that a
#: multi-hundred-MB document does not have to be held in memory.
_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class FileFingerprint:
    content_hash: str
    size: int
    mtime_ns: int


def _stat_signature(path: Path) -> tuple[int, int, int]:
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise FileVanishedError(f"file disappeared: {path.name}") from exc
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def fingerprint(path: Path) -> FileFingerprint:
    """Hash a file, refusing to fingerprint one that is being written.

    A shared folder is live: someone may be saving over a file while the scan
    reads it. Hashing the bytes alone would happily produce a fingerprint for a
    half-written file and store it as a document revision.

    So the file is stat'ed before and after reading. If size, mtime or ctime
    moved, the read is not trustworthy and the caller is told to retry on a
    later run rather than persist a partial document.
    """
    before = _stat_signature(path)

    digest = hashlib.sha256()
    read_bytes = 0
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(_READ_CHUNK)
                if not block:
                    break
                digest.update(block)
                read_bytes += len(block)
    except FileNotFoundError as exc:
        raise FileVanishedError(f"file disappeared while reading: {path.name}") from exc
    except PermissionError as exc:
        raise FileUnstableError(f"permission denied: {path.name}") from exc

    after = _stat_signature(path)
    if before != after or read_bytes != after[0]:
        raise FileUnstableError(
            f"file changed while being read: {path.name} "
            f"(size {before[0]} -> {after[0]}, read {read_bytes} bytes)"
        )

    return FileFingerprint(digest.hexdigest(), after[0], after[1])
