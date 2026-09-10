"""Standard retrieval metrics, defined once.

Written because the earlier reports printed "R@1" for a number that was
actually Hit@1. With three relevant documents and one of them at rank 1:

    Hit@1    = 1.0      did anything relevant appear?
    Recall@1 = 1/3      how much of what exists did we surface?

Both are legitimate, and Hit@k is the honest metric for "did the user get
something useful at the top". Calling it Recall overstated the result: a
Recall@1 of 1.00 would mean every relevant document fit in one slot, which is
impossible whenever a query has more than one answer.

Precision was also non-standard. It divided by ``min(k, |relevant|)`` -- a
per-query normalisation that flatters queries with few answers. Standard
Precision@k divides by k, and is reported that way here even though a query
with one relevant document can then never exceed 1/3 at k=3.

Binary relevance throughout: the benchmark labels a document relevant or not,
with no graded judgements, so nDCG uses gain 1 for relevant and 0 otherwise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


def hit_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """1.0 if any relevant document is in the top k, else 0.0."""
    relevant = set(relevant)
    return 1.0 if any(doc in relevant for doc in ranked[:k]) else 0.0


def recall_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the relevant set that appears in the top k.

    Bounded above by k/|relevant|, so a query with three answers cannot exceed
    1/3 at k=1. That ceiling is the point: it says how much of the answer the
    user can actually see.
    """
    relevant = set(relevant)
    if not relevant:
        return 0.0
    found = sum(1 for doc in ranked[:k] if doc in relevant)
    return found / len(relevant)


def precision_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the top k that is relevant. Divides by k, not by |relevant|."""
    if k <= 0:
        return 0.0
    relevant = set(relevant)
    return sum(1 for doc in ranked[:k] if doc in relevant) / k


def reciprocal_rank_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """1/rank of the first relevant document within the top k, else 0."""
    relevant = set(relevant)
    for position, doc in enumerate(ranked[:k], start=1):
        if doc in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Discounted gain against the best achievable ordering.

    Binary gain. IDCG is computed over min(k, |relevant|) documents, so a query
    with one answer scores 1.0 when that answer is first -- unlike Precision@k,
    which would cap it at 1/k.
    """
    relevant = set(relevant)
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, doc in enumerate(ranked[:k], start=1)
        if doc in relevant
    )
    ideal = sum(
        1.0 / math.log2(position + 1)
        for position in range(1, min(k, len(relevant)) + 1)
    )
    return dcg / ideal if ideal else 0.0


@dataclass(frozen=True)
class Scores:
    """Every metric for one set of queries, averaged."""

    n: int
    hit_1: float
    hit_3: float
    recall_1: float
    recall_3: float
    precision_1: float
    precision_3: float
    mrr_3: float
    ndcg_3: float

    def as_row(self) -> str:
        return (f"{self.hit_1:6.2f} {self.hit_3:6.2f} {self.recall_1:8.2f} "
                f"{self.recall_3:8.2f} {self.precision_1:6.2f} {self.precision_3:6.2f} "
                f"{self.mrr_3:7.2f} {self.ndcg_3:7.2f}")

    @staticmethod
    def header() -> str:
        return (f"{'Hit@1':>6} {'Hit@3':>6} {'Rec@1':>8} {'Rec@3':>8} "
                f"{'P@1':>6} {'P@3':>6} {'MRR@3':>7} {'nDCG@3':>7}")


def evaluate(outcomes: Sequence[tuple[Sequence[str], Iterable[str]]]) -> Scores:
    """Average every metric over a set of (ranking, relevant set) pairs."""
    n = len(outcomes)
    if not n:
        return Scores(0, *([0.0] * 8))

    def mean(fn) -> float:
        return sum(fn(ranked, relevant) for ranked, relevant in outcomes) / n

    return Scores(
        n=n,
        hit_1=mean(lambda r, rel: hit_at_k(r, rel, 1)),
        hit_3=mean(lambda r, rel: hit_at_k(r, rel, 3)),
        recall_1=mean(lambda r, rel: recall_at_k(r, rel, 1)),
        recall_3=mean(lambda r, rel: recall_at_k(r, rel, 3)),
        precision_1=mean(lambda r, rel: precision_at_k(r, rel, 1)),
        precision_3=mean(lambda r, rel: precision_at_k(r, rel, 3)),
        mrr_3=mean(lambda r, rel: reciprocal_rank_at_k(r, rel, 3)),
        ndcg_3=mean(lambda r, rel: ndcg_at_k(r, rel, 3)),
    )
