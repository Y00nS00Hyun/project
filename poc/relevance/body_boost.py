"""Four ranking strategies for body-level lexical evidence. Evaluation only.

Nothing here is imported by ``src/``. Production ranking is untouched.

The problem being measured: the semantic route scores a document by the cosine
of its best body chunk, plus a small trigram boost on the *title*. A rare token
that lives only in the body -- "Zookeeper" -- has no route to the score at all.
Measured on this corpus, e5 places every document in a 0.78-0.85 band, so two
documents that never mention Zookeeper outrank one that mentions it six times.

Four rules, all of which keep the same three invariants:

  * no lexical hard gate -- every accessible READY document is still returned,
    still in relevance order. Boosts reorder; they never filter.
  * no absolute cosine threshold.
  * the title boost stays at its production value of 0.075.

The fourth invariant is specific to this experiment: body trigram is NOT added
to every query. A previous sweep showed a short query's body trigram is mostly
noise -- with the threshold at 0.20 a two-character query matches somewhere in
almost any long document. So C and D each require *evidence* before boosting,
and the evidence is what differs between them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import psycopg

#: Production value. Set as a transaction-local GUC because `<%` reads it from
#: there, matching what SearchService does.
TRIGRAM_THRESHOLD = 0.20

#: Production value, unchanged by every strategy here.
TITLE_BOOST_WEIGHT = 0.075

#: RRF's rank-smoothing constant, at its usual value. Large relative to the
#: seven documents in this corpus, which is the point: it keeps any single
#: list from dominating purely because it ranked something first.
RRF_K = 60


_COMPONENTS_SQL = """
WITH ranked AS (
    SELECT d.id,
           d.title,
           MAX(1 - (c.embedding <=> %(vector)s::vector))        AS semantic,
           word_similarity(%(query)s, d.title)                  AS title_trigram,
           -- The same expression production's lexical route scores with:
           -- word_similarity over the whole extracted text, not per chunk.
           COALESCE(word_similarity(%(query)s, r.extracted_text), 0) AS body_lexical,
           -- Case-insensitive substring, with runs of whitespace and the
           -- separators Korean file and section names use collapsed to a
           -- single space on both sides. No stemming and no morphological
           -- analysis: this is meant to answer "is this term literally in the
           -- document", and anything cleverer stops answering that.
           (position(
               regexp_replace(lower(%(query)s), '[[:space:]_.,/()\\[\\]-]+', ' ', 'g')
               in
               regexp_replace(lower(r.extracted_text), '[[:space:]_.,/()\\[\\]-]+', ' ', 'g')
           ) > 0) AS body_contains
    FROM documents d
    JOIN document_revisions r
        ON r.id = d.current_revision_id AND r.document_id = d.id AND r.is_ready
    JOIN chunks c
        ON c.document_revision_id = r.id AND c.embedding IS NOT NULL
    WHERE d.is_deleted = FALSE
    GROUP BY d.id, d.title, r.extracted_text
)
SELECT title, semantic, title_trigram, body_lexical, body_contains FROM ranked
"""


@dataclass(frozen=True)
class Components:
    """Every signal available for one document, for one query."""

    title: str
    semantic: float
    title_trigram: float
    body_lexical: float
    body_contains: bool

    @property
    def baseline(self) -> float:
        """What production scores today: semantic plus the title trigram boost."""
        return self.semantic + TITLE_BOOST_WEIGHT * self.title_trigram


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
                    Components(t, float(s), float(tt), float(bl), bool(bc))
                    for t, s, tt, bl, bc in cur.fetchall()
                ]
        return self._cache[query]


def _rank(components: list[Components], score: Callable[[Components], float]) -> list[str]:
    # Title as the tiebreaker so an unchanged ordering is reproducible.
    return [c.title for c in sorted(components, key=lambda c: (-score(c), c.title))]


# ---------------------------------------------------------------------------
# A. Current production
# ---------------------------------------------------------------------------

def strategy_a(scorer: Scorer, query: str) -> list[str]:
    """Semantic + title trigram boost (0.075). The baseline being measured."""
    return _rank(scorer.components(query), lambda c: c.baseline)


# ---------------------------------------------------------------------------
# B. RRF hybrid
# ---------------------------------------------------------------------------

def strategy_b(scorer: Scorer, query: str) -> list[str]:
    """Reciprocal rank fusion of the semantic and lexical orderings.

    Both lists cover the same document set, so nothing is removed -- a document
    with no lexical evidence simply sits at the bottom of that list rather than
    being absent from it. That is a deliberate difference from production's
    lexical route, which filters by the `<%` threshold: filtering here would be
    the hard gate this experiment is forbidden from introducing.
    """
    components = scorer.components(query)
    semantic_rank = {t: i for i, t in enumerate(_rank(components, lambda c: c.baseline))}
    lexical_rank = {t: i for i, t in enumerate(_rank(components, lambda c: c.body_lexical))}

    def score(c: Components) -> float:
        return (
            1.0 / (RRF_K + 1 + semantic_rank[c.title])
            + 1.0 / (RRF_K + 1 + lexical_rank[c.title])
        )

    return _rank(components, score)


# ---------------------------------------------------------------------------
# C. Strong body lexical boost
# ---------------------------------------------------------------------------

def make_strategy_c(threshold: float, boost: float) -> Callable[[Scorer, str], list[str]]:
    """Boost only when body lexical evidence clears a bar.

    The bar exists because body trigram is worthless below it. On this corpus a
    document containing the term scores ~1.0 and one that does not scores ~0.2,
    so there is a wide empty gap to put a threshold in rather than a tuned edge.

    Additive and constant rather than proportional: between 0.9 and 1.0 the
    difference is trigram noise, and scaling by it would order two documents
    that both plainly contain the term by an accident of tokenisation.
    """

    def strategy(scorer: Scorer, query: str) -> list[str]:
        return _rank(
            scorer.components(query),
            lambda c: c.baseline + (boost if c.body_lexical >= threshold else 0.0),
        )

    return strategy


# ---------------------------------------------------------------------------
# D. Exact body match boost
# ---------------------------------------------------------------------------

def make_strategy_d(boost: float) -> Callable[[Scorer, str], list[str]]:
    """Boost only when the normalized query literally occurs in the body.

    Stricter evidence than C and cheaper to explain: either the phrase is in
    the document or it is not. It cannot fire on a near-miss, which is both the
    advantage and the limit -- a paraphrase never triggers it, so a paraphrase
    query is left at exactly the baseline ordering.
    """

    def strategy(scorer: Scorer, query: str) -> list[str]:
        return _rank(
            scorer.components(query),
            lambda c: c.baseline + (boost if c.body_contains else 0.0),
        )

    return strategy


# ---------------------------------------------------------------------------
# C' and D' -- the same rules, plus a selectivity condition
# ---------------------------------------------------------------------------
#
# Measured cause of C's and D's regression on short Korean queries: strength
# and discrimination are not the same thing. "파일 다운로드" scores 1.00 on
# three of seven documents, and "데이터베이스 설정" scores 0.80 on three, because
# common Korean phrases occur verbatim almost everywhere. The boost then fires
# on most of the corpus and simply re-sorts it by noise.
#
# "Zookeeper" scores 1.00 on two of seven. The difference is not how strong the
# match is -- both reach 1.00 -- it is how *few* documents it matches. A term
# present in most documents carries no information about which one to read,
# which is the ordinary IDF argument arriving from measurement rather than from
# theory.
#
# So the evidence has to be both strong and rare. The ceiling is a fraction of
# the candidate set rather than a count, so it does not silently change meaning
# when the corpus grows.

#: At most this fraction of the accessible documents may carry the evidence.
#: Above it, the term is background vocabulary and the boost is suppressed --
#: leaving the baseline ordering exactly as it is today.
DEFAULT_SELECTIVITY = 0.5


def _selective(matches: int, total: int, ceiling: float) -> bool:
    return 0 < matches <= max(1, int(total * ceiling))


def make_strategy_c_selective(
    threshold: float, boost: float, ceiling: float = DEFAULT_SELECTIVITY,
) -> Callable[[Scorer, str], list[str]]:
    """C, but only when few enough documents clear the bar."""

    def strategy(scorer: Scorer, query: str) -> list[str]:
        components = scorer.components(query)
        strong = [c for c in components if c.body_lexical >= threshold]
        fire = _selective(len(strong), len(components), ceiling)
        return _rank(
            components,
            lambda c: c.baseline
            + (boost if fire and c.body_lexical >= threshold else 0.0),
        )

    return strategy


def make_strategy_d_selective(
    boost: float, ceiling: float = DEFAULT_SELECTIVITY,
) -> Callable[[Scorer, str], list[str]]:
    """D, but only when few enough documents contain the phrase."""

    def strategy(scorer: Scorer, query: str) -> list[str]:
        components = scorer.components(query)
        containing = [c for c in components if c.body_contains]
        fire = _selective(len(containing), len(components), ceiling)
        return _rank(
            components,
            lambda c: c.baseline + (boost if fire and c.body_contains else 0.0),
        )

    return strategy
