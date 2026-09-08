"""Search queries.

The whole file is built around one rule:

    ACL is applied *before* retrieval, never after.

Every query starts from an ``eligible`` CTE that resolves the caller's
permissions and the filters, and retrieval only ever runs against rows that CTE
produced. A document the user cannot read is not scored, not ranked, and not
counted -- it never enters the candidate set at all.

All SQL is parameterised. No fragment is ever built by string concatenation
with user input.
"""

from __future__ import annotations

from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row

#: Permission values that grant read access. Allow-only model: a document with
#: no matching permission row is invisible (default deny).
READ_PERMISSIONS = ("READ", "WRITE", "ADMIN")

# ---------------------------------------------------------------------------
# Eligibility
#
# `current_ready_chunks` already encodes "not deleted + current revision +
# READY", but it carries no ACL. This CTE supplies the ACL and the filters, and
# every retrieval query joins the two. The view is never queried on its own.
#
# The revision join here also gives document_year, which the view does not
# expose, so the year filter is applied at candidate-set time rather than after
# retrieval.
# ---------------------------------------------------------------------------
ELIGIBLE_CTE = """
eligible AS (
    SELECT
        d.id                  AS document_id,
        d.current_revision_id AS revision_id,
        d.title               AS title,
        d.file_type           AS file_type,
        d.department_id       AS department_id,
        dep.name              AS department_name,
        d.updated_at          AS updated_at,
        r.document_year       AS document_year,
        r.revision_no         AS revision_no,
        r.created_at          AS revision_created_at,
        -- API Contract has_newer_revision: a newer revision exists but is not
        -- yet READY, so search is still serving the current one.
        (d.latest_revision_id IS DISTINCT FROM d.current_revision_id) AS has_newer_revision,
        COALESCE((
            SELECT jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name)
                             ORDER BY t.name, t.id)
            FROM document_tags dt
            JOIN tags t ON t.id = dt.tag_id
            WHERE dt.document_id = d.id
        ), '[]'::jsonb) AS tags,
        r.extracted_text      AS extracted_text
    FROM documents d
    JOIN document_revisions r
        ON r.id = d.current_revision_id
       AND r.document_id = d.id
    LEFT JOIN departments dep
        ON dep.id = d.department_id
    WHERE d.is_deleted = FALSE
      AND d.current_revision_id IS NOT NULL
      AND r.is_ready = TRUE
      AND EXISTS (
          SELECT 1
          FROM document_permissions p
          LEFT JOIN users u ON u.id = %(user_id)s
          WHERE p.document_id = d.id
            AND p.permission = ANY(%(read_permissions)s)
            AND (
                p.user_id = %(user_id)s
                OR (
                    p.department_id IS NOT NULL
                    AND p.department_id = u.department_id
                )
            )
      )
      AND (%(department_id)s::uuid IS NULL OR d.department_id = %(department_id)s::uuid)
      AND (%(year)s::int IS NULL OR r.document_year = %(year)s::int)
      AND (%(file_type)s::text IS NULL OR d.file_type = %(file_type)s::text)
      AND (
          %(tag_count)s = 0
          OR (
              SELECT count(DISTINCT dt.tag_id)
              FROM document_tags dt
              WHERE dt.document_id = d.id
                AND dt.tag_id = ANY(%(tag_ids)s)
          ) = %(tag_count)s
      )
)
"""

#: Columns every result row carries, so the three paths stay interchangeable.
_DOCUMENT_COLUMNS = """
    e.document_id, e.revision_id, e.title, e.file_type,
    e.department_id, e.department_name, e.updated_at, e.document_year,
    e.revision_no, e.revision_created_at, e.has_newer_revision, e.tags
"""


def _base_params(
    user_id: str,
    department_id: str | None,
    year: int | None,
    tag_ids: Sequence[int],
    file_type: str | None = None,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "read_permissions": list(READ_PERMISSIONS),
        "department_id": department_id,
        "year": year,
        "file_type": file_type,
        "tag_ids": list(tag_ids),
        "tag_count": len(set(tag_ids)),
    }


class SearchRepository:
    """Read-only queries for search. Adds nothing to the ingestion write path."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    # -- browse ------------------------------------------------------------

    def browse(
        self,
        *,
        user_id: str,
        department_id: str | None,
        year: int | None,
        tag_ids: Sequence[int],
        file_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Filter browsing: no retrieval at all.

        Ordering is ``updated_at DESC, document_id ASC`` per API Contract v1.
        The id tie-break is what keeps pagination from duplicating or skipping
        rows when several documents share a timestamp.
        """
        sql = f"""
        WITH {ELIGIBLE_CTE}
        SELECT {_DOCUMENT_COLUMNS},
               NULL::uuid   AS chunk_id,
               NULL::int    AS chunk_index,
               NULL::int    AS paragraph_start,
               NULL::int    AS paragraph_end,
               NULL::text   AS section_title,
               NULL::int    AS page_number,
               NULL::text   AS chunk_text,
               NULL::float8 AS score,
               count(*) OVER () AS total_count
        FROM eligible e
        ORDER BY e.updated_at DESC, e.document_id ASC
        LIMIT %(limit)s OFFSET %(offset)s
        """
        params = _base_params(user_id, department_id, year, tag_ids, file_type)
        params.update({"limit": limit, "offset": offset})
        return self._fetch(sql, params)

    # -- semantic ----------------------------------------------------------

    def semantic_search(
        self,
        *,
        user_id: str,
        query_vector: str,
        department_id: str | None,
        year: int | None,
        tag_ids: Sequence[int],
        file_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Exact cosine search over every eligible chunk.

        Aggregation order matters and is the reason this is one statement:

            all eligible chunks -> MAX score per document -> rank -> paginate

        Taking a global chunk top-K first and de-duplicating afterwards would
        let one document with many strong chunks crowd every other document off
        the page, and would make `total` wrong.

        ``DISTINCT ON (document_id) ... ORDER BY document_id, score DESC`` is
        the per-document MAX, and it keeps the winning chunk's identity so the
        caller can cite and snippet it.
        """
        sql = f"""
        WITH {ELIGIBLE_CTE},
        scored AS (
            SELECT DISTINCT ON (c.document_id)
                   c.document_id,
                   c.chunk_id,
                   c.chunk_index,
                   c.paragraph_start,
                   c.paragraph_end,
                   c.section_title,
                   c.page_number,
                   c.text AS chunk_text,
                   1 - (c.embedding <=> %(query_vector)s::vector) AS score
            FROM current_ready_chunks c
            JOIN eligible e ON e.document_id = c.document_id
            WHERE c.embedding IS NOT NULL
            ORDER BY c.document_id,
                     c.embedding <=> %(query_vector)s::vector,
                     c.chunk_index ASC
        )
        SELECT {_DOCUMENT_COLUMNS},
               s.chunk_id, s.chunk_index, s.paragraph_start, s.paragraph_end,
               s.section_title, s.page_number, s.chunk_text, s.score,
               count(*) OVER () AS total_count
        FROM scored s
        JOIN eligible e ON e.document_id = s.document_id
        ORDER BY s.score DESC, e.document_id ASC
        LIMIT %(limit)s OFFSET %(offset)s
        """
        params = _base_params(user_id, department_id, year, tag_ids, file_type)
        params.update({"query_vector": query_vector, "limit": limit, "offset": offset})
        return self._fetch(sql, params)

    # -- lexical -----------------------------------------------------------

    def lexical_search(
        self,
        *,
        user_id: str,
        query_text: str,
        threshold: float,
        department_id: str | None,
        year: int | None,
        tag_ids: Sequence[int],
        file_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """pg_trgm search over document titles and current-revision body text.

        The ``<%`` operator is what the GIN trigram indexes built by the
        migration accelerate, so the WHERE clause uses it rather than a bare
        ``word_similarity(...) >= x`` comparison, which no index can serve.

        The threshold is a single configured value applied to every query; it
        is set as a transaction-local GUC because ``<%`` reads it from there.
        It is never varied per query and never exposed through the API.

        The matched chunk is looked up separately: a title-only hit still
        returns a document, just without a chunk to cite.
        """
        with self.conn.cursor() as cur:
            # set_config() takes parameters; SET does not. `true` scopes it to
            # this transaction so no other session is affected.
            cur.execute(
                "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                (str(threshold),),
            )

        sql = f"""
        WITH {ELIGIBLE_CTE},
        matched AS (
            SELECT e.document_id,
                   GREATEST(
                       word_similarity(%(query_text)s, e.title),
                       COALESCE(word_similarity(%(query_text)s, e.extracted_text), 0)
                   ) AS score
            FROM eligible e
            WHERE %(query_text)s <%% e.title
               OR %(query_text)s <%% e.extracted_text
        ),
        best_chunk AS (
            SELECT DISTINCT ON (c.document_id)
                   c.document_id, c.chunk_id, c.chunk_index,
                   c.paragraph_start, c.paragraph_end, c.section_title,
                   c.page_number, c.text AS chunk_text
            FROM current_ready_chunks c
            JOIN matched m ON m.document_id = c.document_id
            WHERE %(query_text)s <%% c.text
            ORDER BY c.document_id,
                     word_similarity(%(query_text)s, c.text) DESC,
                     c.chunk_index ASC
        )
        SELECT {_DOCUMENT_COLUMNS},
               b.chunk_id, b.chunk_index, b.paragraph_start, b.paragraph_end,
               b.section_title, b.page_number, b.chunk_text,
               m.score,
               count(*) OVER () AS total_count
        FROM matched m
        JOIN eligible e ON e.document_id = m.document_id
        LEFT JOIN best_chunk b ON b.document_id = m.document_id
        ORDER BY m.score DESC, e.document_id ASC
        LIMIT %(limit)s OFFSET %(offset)s
        """
        params = _base_params(user_id, department_id, year, tag_ids, file_type)
        params.update({"query_text": query_text, "limit": limit, "offset": offset})
        return self._fetch(sql, params)

    # -- helpers -----------------------------------------------------------

    def _fetch(self, sql: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]
        # count(*) OVER () is the pre-LIMIT document count, so pagination
        # reports the true total without a second query.
        total = rows[0]["total_count"] if rows else 0
        return rows, int(total)

    def accessible_document_ids(self, user_id: str) -> set[str]:
        """Documents the user may read. Used by tests to assert the invariant."""
        sql = f"WITH {ELIGIBLE_CTE} SELECT document_id FROM eligible"
        params = _base_params(user_id, None, None, ())
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return {str(row[0]) for row in cur.fetchall()}
