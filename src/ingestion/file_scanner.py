"""Shared-folder discovery.

Read-only by construction: this module opens files and stats them and does
nothing else. The shared folder is the source of truth and the system never
modifies, moves, renames or deletes anything in it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import IngestionConfig
from .exceptions import PathOutsideRootError


@dataclass(frozen=True)
class DiscoveredFile:
    """One file found under the shared root.

    ``absolute_path`` stays internal -- it is used to open the file and is never
    written to the database or returned by an API. ``relative_path`` is the
    canonical identity stored as ``documents.source_path``.
    """

    absolute_path: Path
    relative_path: str
    filename: str
    extension: str
    size: int
    mtime: datetime

    @property
    def title(self) -> str:
        """Display title: filename without its extension."""
        return Path(self.filename).stem


def canonical_relative_path(path: Path, root: Path) -> str:
    """Return the POSIX-style path of ``path`` relative to ``root``.

    Raises :class:`PathOutsideRootError` when the resolved path escapes the
    root. This is the single place that decides whether a path is in scope, and
    it works on *resolved* paths so that ``..`` segments and symlinks cannot
    slip past it.
    """
    resolved_root = root.resolve()
    try:
        resolved = path.resolve()
    except OSError as exc:  # pragma: no cover - unreadable path component
        raise PathOutsideRootError(f"path could not be resolved: {path.name}") from exc

    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        # Do not include the outside path in the message: it may itself be the
        # sensitive part of a traversal attempt.
        raise PathOutsideRootError(
            f"path resolves outside the shared root: {path.name}"
        ) from exc
    return relative.as_posix()


def assert_within_root(path: Path, root: Path) -> Path:
    """Validate and return the resolved path, for opening a stored source_path.

    Called again at read time, not just at scan time: ``documents.source_path``
    is data, and data can be wrong or tampered with.
    """
    canonical_relative_path(path, root)
    return path.resolve()


def resolve_source_path(relative_path: str, root: Path) -> Path:
    """Turn a stored ``source_path`` back into an absolute path, safely."""
    if os.path.isabs(relative_path):
        raise PathOutsideRootError("source_path must be relative to the shared root")
    candidate = root / relative_path
    return assert_within_root(candidate, root)


def scan_files(config: IngestionConfig) -> Iterator[DiscoveredFile]:
    """Walk the shared root and yield discoverable documents.

    Symlinks are not followed by default: a symlink inside the shared folder can
    point anywhere on the host, and following one would let a scan read (and
    index) files the shared folder was never meant to expose.
    """
    root = config.resolved_root
    extensions = {f".{e.lower()}" for e in config.discoverable_extensions}

    for dirpath, dirnames, filenames in os.walk(root, followlinks=config.follow_symlinks):
        current = Path(dirpath)
        if not config.follow_symlinks:
            # Prune symlinked directories so os.walk never descends into them.
            dirnames[:] = [d for d in dirnames if not (current / d).is_symlink()]
        dirnames.sort()

        for filename in sorted(filenames):
            path = current / filename
            if path.suffix.lower() not in extensions:
                continue
            if not config.follow_symlinks and path.is_symlink():
                continue
            try:
                relative = canonical_relative_path(path, root)
            except PathOutsideRootError:
                # A symlink that escapes the root, or a racing rename.
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue

            yield DiscoveredFile(
                absolute_path=path,
                relative_path=relative,
                filename=filename,
                extension=path.suffix.lower().lstrip("."),
                size=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            )
