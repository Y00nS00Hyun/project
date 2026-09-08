"""ACL-aware document detail, revision history and download authorisation.

Sits beside the search backend rather than inside it: search ranks documents,
this answers "may this user see document X, and where is its file". Routers
call this; they never build SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ingestion.exceptions import PathOutsideRootError
from ingestion.file_scanner import resolve_source_path
from search.repository import READ_PERMISSIONS

#: The same permission predicate the search backend uses. Kept textually close
#: to it so the two cannot drift into different definitions of "may read".
_ACL_PREDICATE = """
    EXISTS (
        SELECT 1
        FROM document_permissions p
        LEFT JOIN users u ON u.id = %(user_id)s
        WHERE p.document_id = d.id
          AND p.permission = ANY(%(read_permissions)s)
          AND (
              p.user_id = %(user_id)s
              OR (p.department_id IS NOT NULL AND p.department_id = u.department_id)
          )
    )
"""


@dataclass(frozen=True)
class DownloadTarget:
    """Everything needed to serve a file, resolved after the ACL check."""

    absolute_path: Path
    filename: str
    file_type: str


class DocumentNotVisible(Exception):
    """The document does not exist, or the user may not see it.

    One exception for both cases on purpose: the caller turns it into 404 so a
    client cannot tell the difference.
    """


class DocumentNotDownloadable(Exception):
    """The document is visible but its original file cannot be served."""


class DocumentService:
    def __init__(self, conn: psycopg.Connection, shared_root: Path):
        self.conn = conn
        self.shared_root = shared_root

    # -- detail ------------------------------------------------------------

    def get_detail(self, user_id: str, document_id: str) -> dict[str, Any]:
        row = self._visible_document(user_id, document_id)

        current = self._revision_ref(row["current_revision_id"])
        latest = self._revision_ref(row["latest_revision_id"])
        return {
            "document_id": str(row["id"]),
            "title": row["title"],
            "file_type": row["file_type"],
            "department": (
                {"id": str(row["department_id"]), "name": row["department_name"]}
                if row["department_id"] else None
            ),
            "owner": (
                {"id": str(row["owner_id"]), "name": row["owner_name"]}
                if row["owner_id"] else None
            ),
            "tags": self._tags(document_id),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "current_revision": current,
            "latest_revision": latest,
            "is_searchable": current is not None,
            "downloadable": self._is_downloadable(row),
        }

    def list_revisions(
        self, user_id: str, document_id: str, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Revision history, newest first.

        ACL is checked on the logical document first: without that, an
        unreadable document's processing history would be readable.
        """
        row = self._visible_document(user_id, document_id)
        current_revision_id = row["current_revision_id"]

        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, revision_no, content_hash, file_size, source_mtime,
                       parse_status, parse_result_code, is_ready, created_at,
                       count(*) OVER () AS total_count
                FROM document_revisions
                WHERE document_id = %s
                ORDER BY revision_no DESC
                LIMIT %s OFFSET %s
                """,
                (document_id, limit, offset),
            )
            rows = [dict(r) for r in cur.fetchall()]

        total = rows[0]["total_count"] if rows else self._revision_count(document_id)
        items = [
            {
                "revision_id": str(r["id"]),
                "revision_no": r["revision_no"],
                "content_hash": r["content_hash"],
                "file_size": r["file_size"],
                "source_modified_at": r["source_mtime"],
                "parse_status": r["parse_status"],
                "parse_result_code": r["parse_result_code"],
                # current, not latest: a newer FAILED revision is not current.
                "is_current": (
                    current_revision_id is not None and r["id"] == current_revision_id
                ),
                "is_ready": r["is_ready"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        return items, int(total)

    # -- download ----------------------------------------------------------

    def resolve_download(self, user_id: str, document_id: str) -> DownloadTarget:
        """Authorise, then locate. Never the other way round.

        The ACL check happens before any path is resolved or any file is
        opened, so an unauthorised request cannot cause a filesystem access at
        all. The stored ``source_path`` is then re-validated against the shared
        root -- it is data, and data can be wrong or tampered with.
        """
        row = self._visible_document(user_id, document_id)

        if row["current_revision_id"] is None:
            raise DocumentNotDownloadable("아직 처리되지 않은 문서입니다.")
        if row["missing_since"] is not None:
            raise DocumentNotDownloadable("원본 파일을 공유폴더에서 찾을 수 없습니다.")

        try:
            absolute = resolve_source_path(row["source_path"], self.shared_root)
        except PathOutsideRootError:
            # Refuse rather than serve: a stored path that escapes the root is
            # a security event, not a missing file.
            raise DocumentNotDownloadable("원본 파일을 제공할 수 없습니다.") from None

        if not absolute.is_file():
            raise DocumentNotDownloadable("원본 파일을 공유폴더에서 찾을 수 없습니다.")

        return DownloadTarget(
            absolute_path=absolute,
            # Display name only; never the stored path.
            filename=row["original_filename"],
            file_type=row["file_type"],
        )

    # -- internals ---------------------------------------------------------

    def _visible_document(self, user_id: str, document_id: str) -> dict[str, Any]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(
                    f"""
                    SELECT d.id, d.title, d.file_type, d.original_filename, d.source_path,
                           d.department_id, dep.name AS department_name,
                           d.owner_id, o.name AS owner_name,
                           d.current_revision_id, d.latest_revision_id,
                           d.missing_since, d.created_at, d.updated_at
                    FROM documents d
                    LEFT JOIN departments dep ON dep.id = d.department_id
                    LEFT JOIN users o ON o.id = d.owner_id
                    WHERE d.id = %(document_id)s
                      AND d.is_deleted = FALSE
                      AND {_ACL_PREDICATE}
                    """,
                    {
                        "document_id": document_id,
                        "user_id": user_id,
                        "read_permissions": list(READ_PERMISSIONS),
                    },
                )
            except psycopg.errors.InvalidTextRepresentation:
                # A malformed uuid simply matches nothing.
                raise DocumentNotVisible(document_id) from None
            row = cur.fetchone()
        if row is None:
            raise DocumentNotVisible(document_id)
        return dict(row)

    def _revision_ref(self, revision_id) -> dict[str, Any] | None:
        if revision_id is None:
            return None
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, revision_no, created_at FROM document_revisions WHERE id = %s",
                (revision_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "revision_id": str(row["id"]),
            "revision_no": row["revision_no"],
            "created_at": row["created_at"],
        }

    def _tags(self, document_id: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT t.id, t.name
                FROM document_tags dt JOIN tags t ON t.id = dt.tag_id
                WHERE dt.document_id = %s
                ORDER BY t.name, t.id
                """,
                (document_id,),
            )
            return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

    def _revision_count(self, document_id: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM document_revisions WHERE document_id = %s",
                (document_id,),
            )
            return cur.fetchone()[0]

    @staticmethod
    def _is_downloadable(row: dict[str, Any]) -> bool:
        """Advisory: the file is still checked for real at download time."""
        return row["current_revision_id"] is not None and row["missing_since"] is None


def list_tags(conn: psycopg.Connection, limit: int, offset: int) -> tuple[list[dict], int]:
    """Filter vocabulary. Not ACL-scoped -- see the API doc's Architecture Notes."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, count(*) OVER () AS total FROM tags "
            "ORDER BY name ASC, id ASC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        rows = cur.fetchall()
    total = rows[0][2] if rows else 0
    return [{"id": r[0], "name": r[1]} for r in rows], int(total)


def list_departments(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM departments ORDER BY name ASC, id ASC")
        return [{"id": str(r[0]), "name": r[1]} for r in cur.fetchall()]
