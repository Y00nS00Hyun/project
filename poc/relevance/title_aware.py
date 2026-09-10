"""Title-aware ranking variants, for evaluation only.

Nothing here is imported by ``src/``. The production ranking is untouched --
the point is to measure whether a title signal is worth adding, not to add it
and hope.

The problem being measured: the semantic route scores documents purely by the
cosine distance of their best *body* chunk. ``documents.title`` is never read.
A file named "사용자매뉴얼-윤수현" therefore has no advantage at all for the
query "수현" -- and since e5 similarities sit in a narrow 0.78-0.82 band, it
loses to documents that merely happen to embed slightly closer.

Every variant keeps two rules:

* No absolute cosine threshold. The band is too narrow for a constant to mean
  anything, as the earlier evaluation measured.
* No lexical hard gate. A paraphrase query has no words in common with its
  answer, and gating on lexical overlap deleted 38% of paraphrase recall.

So the title signal is added as a *boost* on top of full semantic recall:
every document the semantic route returns is still returned, only reordered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import psycopg

TRIGRAM_THRESHOLD = 0.20

#: Boost applied when the query appears verbatim in the title.
#:
#: Large enough to clear the whole observed semantic band (0.78-0.82, a spread
#: of ~0.04), because a title match should not merely nudge a document up -- it
#: should outrank every document that has no title match at all. It is added to
#: a cosine-like score in [0, 1], so 1.0 makes the ordering "title matches
#: first, then semantic order" without ever removing a document.
EXACT_TITLE_BOOST = 1.0

#: Boost scaled by pg_trgm word_similarity against the title. Same ceiling as
#: the exact boost so a perfect trigram match ranks with an exact one.
TITLE_TRIGRAM_WEIGHT = 1.0

#: Body lexical evidence is worth less than a title match: a word occurring
#: somewhere in a 200,000-character report says much less than the same word in
#: its name.
BODY_TRIGRAM_WEIGHT = 0.3


_COMPONENTS_SQL = """
SELECT d.title,
       1 - MIN(c.embedding <=> %(vector)s::vector)          AS semantic,
       word_similarity(%(query)s, d.title)                  AS title_trigram,
       (position(%(query)s in d.title) > 0)                 AS title_contains,
       COALESCE(MAX(word_similarity(%(query)s, c.text)), 0) AS body_trigram
FROM documents d
JOIN document_revisions r
    ON r.id = d.current_revision_id AND r.document_id = d.id AND r.is_ready
JOIN chunks c
    ON c.document_revision_id = r.id AND c.embedding IS NOT NULL
WHERE d.is_deleted = FALSE
GROUP BY d.id, d.title
"""


@dataclass(frozen=True)
class Components:
    """Every signal available for one document, for one query."""

    title: str
    semantic: float
    title_trigram: float
    title_contains: bool
    body_trigram: float


class Scorer:
    def __init__(self, conn: psycopg.Connection, embed_query: Callable[[str], list[float]]):
        self.conn = conn
        self.embed_query = embed_query
        self._cache: dict[str, list[Components]] = {}

    def components(self, query: str) -> list[Components]:
        if query not in self._cache:
            vector = "[" + ",".join(f"{v:.6f}" for v in self.embed_query(query)) + "]"
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                    (str(TRIGRAM_THRESHOLD),),
                )
                cur.execute(_COMPONENTS_SQL, {"vector": vector, "query": query})
                self._cache[query] = [
                    Components(t, float(s), float(tt), bool(tc), float(bt))
                    for t, s, tt, tc, bt in cur.fetchall()
                ]
        return self._cache[query]


def _rank(components: list[Components], score: Callable[[Components], float]) -> list[str]:
    # Title as the tiebreaker so an unchanged ordering is reproducible.
    return [c.title for c in sorted(components, key=lambda c: (-score(c), c.title))]


def strategy_a_semantic_only(scorer: Scorer, query: str) -> list[str]:
    """A. Current production: best body chunk's cosine, nothing else."""
    return _rank(scorer.components(query), lambda c: c.semantic)


def strategy_b_exact_title(scorer: Scorer, query: str) -> list[str]:
    """B. Semantic, plus a flat boost when the query is a substring of the title.

    Simple and predictable, but blind to near matches: "요구사항 정의서" is not
    a substring of "요구사항_정의서_윤수현" because of the underscores.
    """
    return _rank(
        scorer.components(query),
        lambda c: c.semantic + (EXACT_TITLE_BOOST if c.title_contains else 0.0),
    )


def strategy_c_title_trigram(scorer: Scorer, query: str) -> list[str]:
    """C. Semantic, plus a boost proportional to title trigram similarity.

    Handles separators and partial names, which is what B misses.
    """
    return _rank(
        scorer.components(query),
        lambda c: c.semantic + TITLE_TRIGRAM_WEIGHT * c.title_trigram,
    )


def strategy_d_field_aware(scorer: Scorer, query: str) -> list[str]:
    """D. Title trigram, body trigram and semantic, weighted by field.

    An exact title substring still gets the flat boost on top: containing the
    query outright is stronger evidence than any trigram score.
    """

    def score(c: Components) -> float:
        return (
            c.semantic
            + TITLE_TRIGRAM_WEIGHT * c.title_trigram
            + BODY_TRIGRAM_WEIGHT * c.body_trigram
            + (EXACT_TITLE_BOOST if c.title_contains else 0.0)
        )

    return _rank(scorer.components(query), score)


STRATEGIES: dict[str, tuple[str, Callable[[Scorer, str], list[str]]]] = {
    "A": ("Semantic only (현재)", strategy_a_semantic_only),
    "B": ("+ exact title substring", strategy_b_exact_title),
    "C": ("+ title trigram", strategy_c_title_trigram),
    "D": ("field-aware (title+body+semantic)", strategy_d_field_aware),
}
