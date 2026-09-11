"""The shared folder's structure, as the current user is allowed to see it.

Derived from ``documents.source_path`` -- there is no folders table, and the
shared folder stays the source of truth. Only documents the user may READ
contribute, so a folder whose contents are all invisible does not appear at
all: not greyed out, not empty, absent. The name of a confidential project is
itself information.

This is navigation, not a search facet. The tree answers "what can I browse",
so it is built from ACL plus current-READY only. Year, document kind, file type
and the query narrow the *results*, never the tree -- a folder holding only
manuals must not vanish because the user ticked "report", it must simply return
nothing when selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import psycopg
from psycopg.rows import dict_row

from ingestion.path_encoding import display_name

from .repository import READ_ACL_PREDICATE, READ_PERMISSIONS

# ---------------------------------------------------------------------------
# ACL first, then folders.
#
# The eligible CTE is the same shape the search queries use: permission,
# not-deleted and current-READY. Folder paths are expanded *from its output*,
# so an unreadable document cannot contribute a path segment. Building the
# whole tree and hiding branches in the client would leak every folder name.
# ---------------------------------------------------------------------------
_FOLDER_TREE_SQL = f"""
WITH eligible AS (
    SELECT d.source_path
    FROM documents d
    JOIN document_revisions r
        ON r.id = d.current_revision_id
       AND r.document_id = d.id
    WHERE d.is_deleted = FALSE
      AND d.current_revision_id IS NOT NULL
      AND r.is_ready = TRUE
      AND {READ_ACL_PREDICATE}
),
segments AS (
    SELECT string_to_array(source_path, '/') AS parts FROM eligible
),
folders AS (
    -- Every ancestor of every eligible document. array_length - 1 drops the
    -- file name: a document contributes its folders, not itself.
    SELECT array_to_string(parts[1:depth], '/') AS path,
           parts[depth]                         AS name_segment,
           depth,
           CASE WHEN depth = 1 THEN NULL
                ELSE array_to_string(parts[1:depth - 1], '/')
           END AS parent_path
    FROM segments,
         generate_series(1, array_length(parts, 1) - 1) AS depth
)
SELECT path, name_segment, depth, parent_path, count(*) AS document_count
FROM folders
GROUP BY path, name_segment, depth, parent_path
ORDER BY depth, path
"""


#: The same eligible set the tree is built from, counted instead of expanded.
#:
#: The tree cannot supply this number: documents sitting at the top of the
#: shared folder contribute no folder row at all, so summing the depth-1 counts
#: would silently omit them -- which is exactly the corpus shape that made the
#: sidebar look empty in the first place.
_BROWSABLE_COUNT_SQL = f"""
SELECT count(*)
FROM documents d
JOIN document_revisions r
    ON r.id = d.current_revision_id
   AND r.document_id = d.id
WHERE d.is_deleted = FALSE
  AND d.current_revision_id IS NOT NULL
  AND r.is_ready = TRUE
  AND {READ_ACL_PREDICATE}
"""


@dataclass(frozen=True)
class FolderNode:
    """One folder the caller may browse.

    ``path`` is the identity: canonical, reversible, and what a folder filter
    sends back. ``name`` is for reading only. They differ whenever the folder
    was created outside UTF-8, and a client that rebuilt a path by joining
    names would produce something that matches nothing.
    """

    path: str
    name: str
    depth: int
    parent_path: str | None
    #: Documents anywhere beneath this folder, so a parent counts its children.
    document_count: int


def folder_tree(
    connection_factory: Callable[[], psycopg.Connection], user_id: str
) -> list[FolderNode]:
    """Folders containing at least one document this user may read."""
    if not user_id:
        return []

    with connection_factory() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _FOLDER_TREE_SQL,
                {"user_id": user_id, "read_permissions": list(READ_PERMISSIONS)},
            )
            rows = cur.fetchall()

    return [
        FolderNode(
            path=row["path"],
            # Rendered per segment, not by decoding the whole path: a legacy
            # folder may sit beside a UTF-8 one.
            name=display_name(row["name_segment"]),
            depth=row["depth"],
            parent_path=row["parent_path"],
            document_count=row["document_count"],
        )
        for row in rows
    ]


def browsable_document_count(
    connection_factory: Callable[[], psycopg.Connection], user_id: str
) -> int:
    """How many documents this user may browse, folders or not.

    Used for the root row of the tree, which stands for the whole shared folder
    rather than for any one directory.
    """
    if not user_id:
        return 0

    with connection_factory() as conn, conn.cursor() as cur:
        cur.execute(
            _BROWSABLE_COUNT_SQL,
            {"user_id": user_id, "read_permissions": list(READ_PERMISSIONS)},
        )
        return cur.fetchone()[0]
