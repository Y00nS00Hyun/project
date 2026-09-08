"""Lexical search methods: PostgreSQL `simple` FTS and pg_trgm.

Both are deliberately run with a single fixed configuration for every query --
no per-query threshold or weight tuning (요청 sections 16, 29.5).
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class ScoredDocument:
    document_id: str
    score: float


class SimpleFTS:
    """PostgreSQL full-text search with the `simple` configuration.

    `simple` does not stem and has no Korean dictionary: it lowercases and
    splits on non-word characters. A Korean noun carrying a particle
    ("개인정보를") therefore becomes a different token from the bare noun,
    which is exactly the failure mode this PoC is meant to expose.
    """

    name = "fts_simple"

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def search(self, query: str, top_k: int = 10) -> list[ScoredDocument]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id,
                       ts_rank_cd(search_vector, plainto_tsquery('simple', %s)) AS score
                FROM poc_documents
                WHERE search_vector @@ plainto_tsquery('simple', %s)
                ORDER BY score DESC, document_id ASC
                LIMIT %s
                """,
                (query, query, top_k),
            )
            return [ScoredDocument(row[0], float(row[1])) for row in cur.fetchall()]


class TrigramSearch:
    """pg_trgm similarity search.

    Uses ``word_similarity(query, content)``: it scores the best-matching
    word-boundary window inside the document rather than comparing the whole
    document to the query, which would be diluted to near zero on any document
    longer than the query.

    One global floor is applied to every query; nothing is tuned per query.
    """

    name = "trigram"

    #: Single global floor, identical for all queries.
    SIMILARITY_FLOOR = 0.20

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def search(self, query: str, top_k: int = 10) -> list[ScoredDocument]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT document_id,
                       GREATEST(
                           word_similarity(%s, title),
                           word_similarity(%s, content)
                       ) AS score
                FROM poc_documents
                WHERE GREATEST(
                          word_similarity(%s, title),
                          word_similarity(%s, content)
                      ) >= %s
                ORDER BY score DESC, document_id ASC
                LIMIT %s
                """,
                (query, query, query, query, self.SIMILARITY_FLOOR, top_k),
            )
            return [ScoredDocument(row[0], float(row[1])) for row in cur.fetchall()]

class SimpleFTSOr(SimpleFTS):
    """`simple` FTS with OR semantics instead of AND.

    ``plainto_tsquery`` joins terms with AND, so a single unmatched token drops
    the document entirely -- which in Korean happens constantly, because a noun
    carrying a particle is a different token. This variant exists to separate
    two distinct causes of FTS failure:

        AND-strictness   -> fixed by OR
        tokenisation     -> not fixed by OR

    Without it, the two are indistinguishable in the results.
    """

    name = "fts_simple_or"

    def search(self, query: str, top_k: int = 10) -> list[ScoredDocument]:
        # Build an OR query from the same tokens `simple` would produce.
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT array_to_string(tsvector_to_array(to_tsvector('simple', %s)), ' | ')",
                (query,),
            )
            row = cur.fetchone()
            tsquery = row[0] if row else ""
            if not tsquery:
                return []
            cur.execute(
                """
                SELECT document_id,
                       ts_rank_cd(search_vector, to_tsquery('simple', %s)) AS score
                FROM poc_documents
                WHERE search_vector @@ to_tsquery('simple', %s)
                ORDER BY score DESC, document_id ASC
                LIMIT %s
                """,
                (tsquery, tsquery, top_k),
            )
            return [ScoredDocument(r[0], float(r[1])) for r in cur.fetchall()]
