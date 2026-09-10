"""Validating and matching folder paths from the shared folder.

A folder path is a prefix of ``documents.source_path``: the same canonical,
reversible form, so it is always valid UTF-8 and always relative to the shared
root. The server absolute path never appears here or in any response.

Two things need care.

``%`` and ``_`` are SQL LIKE wildcards, and both occur in real canonical paths:
``%`` introduces a byte escape (a Korean folder written from Windows is full of
them) and ``_`` is ordinary in file names. Matching a prefix without escaping
them would turn ``%C7%C1`` into a wildcard that matches almost everything.

The prefix boundary is the separator, not the string. ``2026`` must not match
``20260``.
"""

from __future__ import annotations

from ingestion.path_encoding import is_canonical

from .exceptions import InvalidSearchRequestError

#: Escape character for LIKE. Backslash is not special in a POSIX file name,
#: but it is escaped first regardless so the escaping itself round-trips.
LIKE_ESCAPE = "\\"

_LIKE_WILDCARDS = ("%", "_")


def escape_like(text: str) -> str:
    """Make ``text`` match literally under ``LIKE ... ESCAPE '\\'``.

    Backslash first: escaping it after the wildcards would double-escape the
    backslashes this function just introduced.
    """
    escaped = text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
    for wildcard in _LIKE_WILDCARDS:
        escaped = escaped.replace(wildcard, LIKE_ESCAPE + wildcard)
    return escaped


def normalize_folder_path(raw: str | None) -> str | None:
    """Validate a caller-supplied folder path, or None for "no folder".

    Rejects rather than repairs. A path that has to be repaired is one the
    client did not get from :func:`folder_tree`, and quietly reinterpreting it
    would search somewhere the user did not ask for.
    """
    if raw is None:
        return None

    path = raw.strip()
    if not path:
        return None

    if path.startswith("/"):
        raise InvalidSearchRequestError("folder_path must be relative to the shared root")

    # Windows-style separators are not what the canonical form uses, and
    # accepting them would create two spellings of one folder.
    if "\\" in path:
        raise InvalidSearchRequestError("folder_path must use '/' as the separator")

    segments = path.split("/")
    if any(segment == "" for segment in segments):
        raise InvalidSearchRequestError("folder_path must not contain empty segments")
    if any(segment in (".", "..") for segment in segments):
        raise InvalidSearchRequestError("folder_path must not contain '.' or '..'")

    if not is_canonical(path):
        # Not something folder_tree produced. Escapes here are load-bearing --
        # they name raw bytes -- so a malformed one is not a path at all.
        raise InvalidSearchRequestError("folder_path is not a valid canonical path")

    return path


def subtree_prefix(folder_path: str) -> str:
    """The LIKE pattern matching every document inside ``folder_path``.

    The trailing separator is what makes the boundary exact: without it,
    ``프로젝트_A`` would also match ``프로젝트_A2``.

    The caller appends the wildcard in SQL (``... || '%'``) so the folder path
    itself stays a bound parameter and is never concatenated into the query.
    """
    return escape_like(folder_path.rstrip("/") + "/")
