"""Retrieval strategies compared by the evaluation.

Evaluation-only. Nothing here is imported by `src/`, and the production search
policy is untouched -- the point of the exercise is to decide what that policy
should be, not to change it first.

Two rules held throughout:

* No absolute cosine threshold. The measured similarities sit in a narrow band
  (an unrelated query scores 0.81 while a relevant one scores 0.77), so any
  constant would be a number picked to fit seven documents.
* No score leaves these functions. They return document titles in rank order,
  which is exactly what the API exposes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import psycopg

#: Same value the production lexical route configures.
TRIGRAM_THRESHOLD = 0.20

# ---------------------------------------------------------------------------
# Candidate generation
#
# Both routes aggregate to the document with MAX over its chunks, matching the
# production rule (never top-K chunks then dedupe).
# ---------------------------------------------------------------------------

_SEMANTIC_SQL = """
SELECT d.title,
       MIN(c.embedding <=> %(vector)s::vector) AS distance
FROM documents d
JOIN document_revisions r ON r.id = d.current_revision_id AND r.is_ready
JOIN chunks c ON c.document_revision_id = r.id
WHERE d.is_deleted = FALSE AND c.embedding IS NOT NULL
GROUP BY d.id, d.title
ORDER BY distance ASC, d.title ASC
"""

_LEXICAL_SQL = """
SELECT d.title,
       MAX(word_similarity(%(query)s, c.text)) AS score
FROM documents d
JOIN document_revisions r ON r.id = d.current_revision_id AND r.is_ready
JOIN chunks c ON c.document_revision_id = r.id
WHERE d.is_deleted = FALSE
  AND %(query)s <%% c.text
GROUP BY d.id, d.title
ORDER BY score DESC, d.title ASC
"""


@dataclass
class Ranked:
    """A ranking, plus the raw ordering each route produced."""

    titles: list[str]

    def top(self, k: int) -> list[str]:
        return self.titles[:k]


class Engine:
    """Runs the two retrieval routes against the live database."""

    def __init__(self, conn: psycopg.Connection, embed_query: Callable[[str], list[float]]):
        self.conn = conn
        self.embed_query = embed_query
        self._vector_cache: dict[str, str] = {}

    def _vector(self, query: str) -> str:
        if query not in self._vector_cache:
            values = self.embed_query(query)
            self._vector_cache[query] = "[" + ",".join(f"{v:.6f}" for v in values) + "]"
        return self._vector_cache[query]

    def semantic(self, query: str) -> list[str]:
        """Every eligible document, ordered by best-chunk cosine distance.

        No cut-off: this is exactly what the production default does today, and
        it is why an unrelated query still returns the whole corpus.
        """
        rows = self.conn.execute(_SEMANTIC_SQL, {"vector": self._vector(query)}).fetchall()
        return [r[0] for r in rows]

    def lexical(self, query: str) -> list[str]:
        """Documents whose text actually contains the query's words.

        pg_trgm's `<%` with the configured word_similarity threshold. Returns
        nothing when no document shares vocabulary with the query.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                (str(TRIGRAM_THRESHOLD),),
            )
            cur.execute(_LEXICAL_SQL, {"query": query})
            return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def strategy_a_semantic_only(engine: Engine, query: str) -> list[str]:
    """A. Semantic only -- the current production default."""
    return engine.semantic(query)


def strategy_b_lexical_only(engine: Engine, query: str) -> list[str]:
    """B. Lexical only -- pg_trgm word similarity."""
    return engine.lexical(query)


def strategy_c_lexical_gate(engine: Engine, query: str) -> list[str]:
    """C. Lexical gate, semantic rerank.

    The lexical hits are the candidate set; semantic order decides the ranking
    inside it. No lexical hit means no result at all.
    """
    gate = set(engine.lexical(query))
    if not gate:
        return []
    return [title for title in engine.semantic(query) if title in gate]


def _rrf(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Reciprocal rank fusion.

    Rank-based, so the two routes' incomparable score scales never have to be
    reconciled -- which is what makes this safe to use without inventing a
    threshold. k=60 is the value from the original RRF paper, not tuned here.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, title in enumerate(ranking, start=1):
            scores[title] = scores.get(title, 0.0) + 1.0 / (k + position)
    return [title for title, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def strategy_d_union_rrf(engine: Engine, query: str) -> list[str]:
    """D. Union of both routes, fused by reciprocal rank.

    Everything either route found, ordered by RRF. Recall is the union's, so
    nothing semantic could reach is lost -- and nothing is suppressed either.
    """
    return _rrf([engine.semantic(query), engine.lexical(query)])


def strategy_d2_lexical_first_rrf(engine: Engine, query: str) -> list[str]:
    """D2. Fusion, but only when the query has lexical footing.

    A variant, not a new ranking model: the same RRF as D, gated by "did any
    document actually contain these words". It exists to separate two effects
    that D conflates -- fusion's ordering benefit, and the gate's suppression.
    """
    lexical = engine.lexical(query)
    if not lexical:
        return []
    return _rrf([engine.semantic(query), lexical])


STRATEGIES: dict[str, tuple[str, Callable[[Engine, str], list[str]]]] = {
    "A": ("Semantic only (현재 기본)", strategy_a_semantic_only),
    "B": ("Lexical only (pg_trgm)", strategy_b_lexical_only),
    "C": ("Lexical gate + semantic rerank", strategy_c_lexical_gate),
    "D": ("Union + RRF fusion", strategy_d_union_rrf),
    "D2": ("Lexical gate + RRF fusion", strategy_d2_lexical_first_rrf),
}
