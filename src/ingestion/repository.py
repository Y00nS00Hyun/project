"""Data access for ingestion.

Explicit SQL over psycopg, matching the direction already set by the migration
(raw DDL, no ORM). Only the queries ingestion actually needs are implemented --
this is not a generic CRUD layer.

Every statement here is written against docs/database-schema-v2.5.md. Status and
result-code values come from that schema's CHECK constraints; none are invented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

#: processing_jobs.job_type value for the parsing stage.
JOB_TYPE_PARSE = "PARSE"

#: Statuses that make a job "active"; matches the uq_jobs_active partial index.
ACTIVE_JOB_STATUSES = ("PENDING", "RUNNING")


@dataclass(frozen=True)
class DocumentRow:
    id: str
    source_path: str
    title: str
    file_type: str
    latest_revision_id: str | None
    current_revision_id: str | None
    is_deleted: bool
    missing_since: datetime | None


@dataclass(frozen=True)
class RevisionRow:
    id: str
    document_id: str
    revision_no: int
    content_hash: str
    parse_status: str
    parse_result_code: str | None
    is_ready: bool


def _document_row(row: dict[str, Any]) -> DocumentRow:
    return DocumentRow(
        id=str(row["id"]),
        source_path=row["source_path"],
        title=row["title"],
        file_type=row["file_type"],
        latest_revision_id=str(row["latest_revision_id"]) if row["latest_revision_id"] else None,
        current_revision_id=str(row["current_revision_id"]) if row["current_revision_id"] else None,
        is_deleted=row["is_deleted"],
        missing_since=row["missing_since"],
    )


class IngestionRepository:
    """Queries used by the sync and ingestion services.

    Takes a connection rather than owning one: transaction boundaries belong to
    the service, which knows which steps must commit together.
    """

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # -- documents ---------------------------------------------------------

    def find_document_by_source_path(self, source_path: str) -> DocumentRow | None:
        """Look up a document by its canonical relative path.

        Includes soft-deleted rows: a file reappearing at a known path should
        revive its document rather than create a duplicate.
        """
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, source_path, title, file_type, latest_revision_id,
                       current_revision_id, is_deleted, missing_since
                FROM documents
                WHERE source_path = %s
                ORDER BY is_deleted ASC, created_at ASC
                LIMIT 1
                """,
                (source_path,),
            )
            row = cur.fetchone()
        return _document_row(row) if row else None

    def create_document(
        self, *, title: str, original_filename: str, source_path: str, file_type: str
    ) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (title, original_filename, source_path, file_type, last_seen_at)
                VALUES (%s, %s, %s, %s, now())
                RETURNING id
                """,
                (title, original_filename, source_path, file_type),
            )
            return str(cur.fetchone()[0])

    def touch_document(self, document_id: str) -> None:
        """Record that the file was seen, clearing any missing/deleted state.

        Reviving on reappearance is deterministic: the same path with a live
        file is never left marked missing.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET last_seen_at = now(),
                    missing_since = NULL,
                    is_deleted = FALSE,
                    deleted_at = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (document_id,),
            )

    def mark_missing(self, document_id: str) -> None:
        """First miss: start the grace period. Does not delete anything."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET missing_since = COALESCE(missing_since, now()), updated_at = now()
                WHERE id = %s AND is_deleted = FALSE
                """,
                (document_id,),
            )

    def soft_delete_expired_missing(self, grace_seconds: int) -> list[str]:
        """Soft-delete documents missing longer than the grace period.

        Never a hard delete: revisions, chunks and past answer provenance stay.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET is_deleted = TRUE, deleted_at = now(), updated_at = now()
                WHERE is_deleted = FALSE
                  AND missing_since IS NOT NULL
                  AND missing_since <= now() - make_interval(secs => %s)
                RETURNING id
                """,
                (grace_seconds,),
            )
            return [str(r[0]) for r in cur.fetchall()]

    def active_document_paths(self) -> dict[str, str]:
        """Map source_path -> document_id for every non-deleted document."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT source_path, id FROM documents WHERE is_deleted = FALSE")
            return {row[0]: str(row[1]) for row in cur.fetchall()}

    # -- revisions ---------------------------------------------------------

    def latest_revision(self, document_id: str) -> RevisionRow | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, document_id, revision_no, content_hash,
                       parse_status, parse_result_code, is_ready
                FROM document_revisions
                WHERE document_id = %s
                ORDER BY revision_no DESC
                LIMIT 1
                """,
                (document_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return RevisionRow(
            id=str(row["id"]),
            document_id=str(row["document_id"]),
            revision_no=row["revision_no"],
            content_hash=row["content_hash"],
            parse_status=row["parse_status"],
            parse_result_code=row["parse_result_code"],
            is_ready=row["is_ready"],
        )

    def create_revision(
        self,
        *,
        document_id: str,
        content_hash: str,
        file_size: int,
        source_mtime: datetime,
        source_path_at_ingest: str,
        document_year: int | None,
    ) -> str:
        """Create the next revision for a document.

        ``revision_no`` is derived inside the statement so two concurrent
        writers cannot both compute the same number; the
        UNIQUE (document_id, revision_no) constraint then rejects the loser.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_revisions
                    (document_id, revision_no, content_hash, file_size, source_mtime,
                     source_path_at_ingest, document_year)
                VALUES (
                    %(document_id)s,
                    (SELECT COALESCE(MAX(revision_no), 0) + 1
                     FROM document_revisions WHERE document_id = %(document_id)s),
                    %(content_hash)s, %(file_size)s, %(source_mtime)s,
                    %(source_path_at_ingest)s, %(document_year)s
                )
                RETURNING id
                """,
                {
                    "document_id": document_id,
                    "content_hash": content_hash,
                    "file_size": file_size,
                    "source_mtime": source_mtime,
                    "source_path_at_ingest": source_path_at_ingest,
                    "document_year": document_year,
                },
            )
            return str(cur.fetchone()[0])

    def set_latest_revision(self, document_id: str, revision_id: str) -> None:
        """Point latest_revision_id at a revision.

        current_revision_id is deliberately untouched: promotion happens only
        when a revision becomes READY, which requires embedding -- a later
        stage. Until then latest != current is the correct, expected state.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET latest_revision_id = %s, updated_at = now() WHERE id = %s",
                (revision_id, document_id),
            )

    def revision_processing_state(self, revision_id: str) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT r.id, r.document_id, r.parse_status, r.parse_result_code,
                       r.embedding_status, r.is_ready, r.source_path_at_ingest,
                       d.source_path, d.file_type, d.title
                FROM document_revisions r
                JOIN documents d ON d.id = r.document_id
                WHERE r.id = %s
                """,
                (revision_id,),
            )
            return cur.fetchone()

    def save_parse_result(
        self,
        *,
        revision_id: str,
        parse_status: str,
        parse_result_code: str,
        extracted_text: str | None,
        parsed_structure: dict[str, Any] | None,
        parser_name: str | None,
        parser_version: str | None,
        chunking_version: str | None,
        downstream_status: str,
    ) -> None:
        """Persist the parse outcome and the resulting downstream state.

        ``downstream_status`` is applied to embedding/summary/tagging. It is
        SKIPPED when no body text was obtained, and left PENDING when it was --
        embedding is a later stage and must not be faked into SUCCESS, because
        that would make is_ready true and expose an unembedded revision to search.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_revisions
                SET parse_status = %s,
                    parse_result_code = %s,
                    extracted_text = %s,
                    parsed_structure = %s,
                    parser_name = %s,
                    parser_version = %s,
                    chunking_version = %s,
                    embedding_status = %s,
                    summary_status = %s,
                    tagging_status = %s
                WHERE id = %s
                """,
                (
                    parse_status,
                    parse_result_code,
                    extracted_text,
                    json.dumps(parsed_structure, ensure_ascii=False) if parsed_structure else None,
                    parser_name,
                    parser_version,
                    chunking_version,
                    downstream_status,
                    downstream_status,
                    downstream_status,
                    revision_id,
                ),
            )

    # -- processing jobs ---------------------------------------------------

    def enqueue_parse_job(self, revision_id: str) -> str | None:
        """Create a PENDING PARSE job unless one is already active.

        Idempotency comes from the schema's uq_jobs_active partial unique index
        (document_revision_id, job_type) WHERE status IN ('PENDING','RUNNING'),
        so a repeated scan cannot pile up duplicate work -- and neither can two
        concurrent scanners.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO processing_jobs (document_revision_id, job_type, status)
                VALUES (%s, %s, 'PENDING')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (revision_id, JOB_TYPE_PARSE),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    def claim_parse_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Atomically take PENDING PARSE jobs and mark them RUNNING.

        ``FOR UPDATE SKIP LOCKED`` lets several workers share the queue without
        a distributed lock and without handing the same job to two of them.
        """
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE processing_jobs
                SET status = 'RUNNING',
                    attempt_count = attempt_count + 1,
                    started_at = now()
                WHERE id IN (
                    SELECT id FROM processing_jobs
                    WHERE job_type = %s AND status = 'PENDING'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                      AND attempt_count < max_attempts
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                RETURNING id, document_revision_id, attempt_count, max_attempts
                """,
                (JOB_TYPE_PARSE, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def finish_job(
        self, job_id: str, *, status: str, result_code: str | None, error_message: str | None = None
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE processing_jobs
                SET status = %s, result_code = %s, error_message = %s, finished_at = now()
                WHERE id = %s
                """,
                (status, result_code, error_message, job_id),
            )

    def reset_job_for_retry(self, job_id: str) -> None:
        """Return a failed job to the queue.

        No backoff scheduler here -- that is a later operational concern. This
        only makes retry structurally possible.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE processing_jobs
                SET status = 'PENDING', finished_at = NULL, next_attempt_at = NULL
                WHERE id = %s AND status = 'FAILED' AND attempt_count < max_attempts
                """,
                (job_id,),
            )

    # -- chunks ------------------------------------------------------------

    def replace_chunks(self, revision_id: str, chunks: Sequence[Any]) -> int:
        """Delete-then-insert all chunks of a revision.

        Rebuild rather than upsert: a re-parse can produce *fewer* chunks than
        before, and upserting would leave orphaned tail chunks from the previous
        run mixed in with the new ones. Both statements run in the caller's
        transaction, so a revision is never left half-chunked.
        """
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE document_revision_id = %s", (revision_id,))
            if not chunks:
                return 0
            cur.executemany(
                """
                INSERT INTO chunks
                    (document_revision_id, chunk_index, text, page_number,
                     paragraph_start, paragraph_end, section_title, token_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        revision_id, c.chunk_index, c.text, c.page_number,
                        c.paragraph_start, c.paragraph_end, c.section_title, c.token_count,
                    )
                    for c in chunks
                ],
            )
            return len(chunks)

    def count_chunks(self, revision_id: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM chunks WHERE document_revision_id = %s", (revision_id,)
            )
            return cur.fetchone()[0]


def iter_batches(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
