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


READ_ACL_PREDICATE = """
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
ELIGIBLE_CTE = f"""
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
      AND {READ_ACL_PREDICATE}
      -- Document scope, for a chat session bound to one document. It sits
      -- beside the ACL rather than in the filter block below because it is
      -- the same kind of rule: not a preference the caller expressed, but a
      -- boundary the caller cannot cross. A scoped session that reaches this
      -- CTE can produce candidates from exactly one document, so no prompt
      -- wording and no later filtering step can widen it.
      AND (%(scope_document_id)s::uuid IS NULL OR d.id = %(scope_document_id)s::uuid)
      AND (%(department_id)s::uuid IS NULL OR d.department_id = %(department_id)s::uuid)
      AND (%(year)s::int IS NULL OR r.document_year = %(year)s::int)
      AND (%(file_type)s::text IS NULL OR d.file_type = %(file_type)s::text)
      -- Folder subtree. The pattern arrives already LIKE-escaped and ending in
      -- the separator, so selecting "2026" cannot pull in "20260", and the
      -- percent signs a canonical path uses for byte escapes stay literal.
      AND (
          %(folder_prefix)s::text IS NULL
          OR d.source_path LIKE %(folder_prefix)s::text || '%%' ESCAPE '\\'
      )
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

# ---------------------------------------------------------------------------
# Snippet selection
#
# The semantic-nearest chunk is the wrong snippet when the query is not really
# about the body. Searching "수현" -- a name that appears in three file names
# and almost nowhere in the text -- returned "(서명) |", "Copyright © | 개정
# 이력" and a row of table pipes, because short featureless chunks land close
# to anything in embedding space.
#
# So the snippet is chosen in its own right:
#
#   1. a chunk that actually contains the query, best trigram score first
#   2. otherwise the semantic-nearest chunk (the previous behaviour)
#   3. and if that chunk is a table skeleton or a stub, the document's first
#      substantial chunk instead
#
# "Substantial" is deliberately crude: length, and how much of the text is
# letters or digits rather than separators. Measured on this corpus, table
# skeletons score around 0.20 while ordinary prose scores 0.50-0.75, so the
# threshold sits in a wide empty gap rather than on a tuned edge.
MIN_SNIPPET_CHARS = 20

#: Fraction of a chunk that must be letters or digits.
MIN_SNIPPET_WORD_RATIO = 0.35


#: A short chunk carrying a table separator is a row fragment or a boilerplate
#: line ("Copyright © | 개정 이력", "/webapps |"), not an explanation. Longer
#: pipe-bearing chunks are left alone: a full table row can be worth quoting.
#:
#: This makes ~2% of chunks ineligible as a *fallback* snippet. It never
#: removes a document, and a chunk that contains the query is still shown --
#: that branch is checked first and does not consult this predicate.
MAX_SEPARATOR_FRAGMENT_CHARS = 40


def _substantial(column: str) -> str:
    """SQL predicate: is this chunk worth showing a person?

    Deliberately crude -- length, how much of it is letters or digits, and
    whether it is a short separator fragment. Measured on the verification
    corpus: table skeletons score around 0.20 on the word ratio while ordinary
    prose scores 0.50-0.75, so the threshold sits in a wide empty gap.
    """
    return f"""(
        length(btrim({column})) >= {MIN_SNIPPET_CHARS}
        AND (length({column})
             - length(regexp_replace({column}, '[가-힣A-Za-z0-9]', '', 'g')))::numeric
            / GREATEST(length({column}), 1) >= {MIN_SNIPPET_WORD_RATIO}
        AND NOT (
            length(btrim({column})) < {MAX_SEPARATOR_FRAGMENT_CHARS}
            AND position('|' in {column}) > 0
        )
    )"""


#: Runs once per result row, after ranking -- it only decides what to quote,
#: never which documents come back.
#:
#: It returns nothing when the semantically nearest chunk is already fine and
#: the query appears nowhere in the body, which is case (2): the caller's
#: COALESCE then keeps that chunk.
SNIPPET_LATERAL = f"""
    SELECT c2.id AS chunk_id, c2.chunk_index, c2.paragraph_start, c2.paragraph_end,
           c2.section_title, c2.page_number, c2.text AS chunk_text
    FROM chunks c2
    WHERE c2.document_revision_id = e.revision_id
      AND (
          -- (1) a chunk that actually contains the query
          (%(query_text)s <> '' AND c2.text ILIKE '%%' || %(query_text)s || '%%')
          -- (3) or a readable stand-in, but only when the semantic pick is not
          --     one itself
          OR (NOT {_substantial('s.chunk_text')} AND {_substantial('c2.text')})
      )
    ORDER BY
      -- Containing the query beats merely being readable.
      (%(query_text)s <> '' AND c2.text ILIKE '%%' || %(query_text)s || '%%') DESC,
      CASE WHEN %(query_text)s <> '' AND c2.text ILIKE '%%' || %(query_text)s || '%%'
           THEN word_similarity(%(query_text)s, c2.text) ELSE 0 END DESC,
      c2.chunk_index ASC
    LIMIT 1
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
    folder_prefix: str | None = None,
    scope_document_id: str | None = None,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "read_permissions": list(READ_PERMISSIONS),
        # None for ordinary search. Set only by a document-scoped chat session,
        # and enforced in ELIGIBLE_CTE next to the ACL.
        "scope_document_id": scope_document_id,
        "department_id": department_id,
        "year": year,
        "file_type": file_type,
        # Already LIKE-escaped and separator-terminated by the caller; the
        # query only appends the wildcard.
        "folder_prefix": folder_prefix,
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
        folder_prefix: str | None = None,
        scope_document_id: str | None = None,
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
        params = _base_params(
            user_id, department_id, year, tag_ids, file_type, folder_prefix,
            scope_document_id,
        )
        params.update({"limit": limit, "offset": offset})
        return self._fetch(sql, params)

    # -- semantic ----------------------------------------------------------

    def semantic_search(
        self,
        *,
        user_id: str,
        query_vector: str,
        query_text: str | None = None,
        title_boost_weight: float = 0.0,
        department_id: str | None,
        year: int | None,
        tag_ids: Sequence[int],
        file_type: str | None,
        folder_prefix: str | None = None,
        scope_document_id: str | None = None,
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
               COALESCE(sn.chunk_id, s.chunk_id)               AS chunk_id,
               COALESCE(sn.chunk_index, s.chunk_index)         AS chunk_index,
               COALESCE(sn.paragraph_start, s.paragraph_start) AS paragraph_start,
               COALESCE(sn.paragraph_end, s.paragraph_end)     AS paragraph_end,
               COALESCE(sn.section_title, s.section_title)     AS section_title,
               COALESCE(sn.page_number, s.page_number)         AS page_number,
               COALESCE(sn.chunk_text, s.chunk_text)           AS chunk_text,
               -- Title boost. Additive, so nothing is removed from the result
               -- set: a document whose title has no bearing on the query keeps
               -- its semantic score exactly.
               s.score + %(title_boost_weight)s
                       * word_similarity(%(query_text)s, e.title) AS score,
               count(*) OVER () AS total_count
        FROM scored s
        JOIN eligible e ON e.document_id = s.document_id
        LEFT JOIN LATERAL (
            {SNIPPET_LATERAL}
        ) sn ON TRUE
        ORDER BY score DESC, e.document_id ASC
        LIMIT %(limit)s OFFSET %(offset)s
        """
        params = _base_params(
            user_id, department_id, year, tag_ids, file_type, folder_prefix,
            scope_document_id,
        )
        params.update({
            "query_vector": query_vector,
            "query_text": query_text or "",
            "title_boost_weight": title_boost_weight,
            "limit": limit,
            "offset": offset,
        })
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
        folder_prefix: str | None = None,
        scope_document_id: str | None = None,
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
        params = _base_params(
            user_id, department_id, year, tag_ids, file_type, folder_prefix,
            scope_document_id,
        )
        params.update({"query_text": query_text, "limit": limit, "offset": offset})
        return self._fetch(sql, params)

    # -- helpers -----------------------------------------------------------

    def load_context_chunks(
        self, user_id: str, chunk_ids: Sequence[str], max_chars: int,
    ) -> list[dict[str, Any]]:
        """Hydrate already-ranked matches, rechecking the SAME ACL/current CTE.

        This is not a second retrieval/ranking path. RAG needs actual chunk
        text, not the public 200-character display snippet. Oversized chunks
        are skipped whole so a truncated sentence cannot become evidence.
        """
        if not chunk_ids:
            return []
        params = _base_params(user_id, None, None, ())
        params.update(chunk_ids=list(chunk_ids), max_chars=max_chars)
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"""
                WITH {ELIGIBLE_CTE}
                SELECT e.document_id, e.revision_id, e.title, e.file_type,
                       c.id AS chunk_id, c.text, c.paragraph_start,
                       c.paragraph_end, c.page_number, c.section_title
                FROM eligible e
                JOIN chunks c ON c.document_revision_id = e.revision_id
                WHERE c.id = ANY(%(chunk_ids)s::uuid[])
                  AND char_length(c.text) <= %(max_chars)s
                ORDER BY array_position(%(chunk_ids)s::uuid[], c.id)
            """, params)
            return [dict(row) for row in cur.fetchall()]

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
