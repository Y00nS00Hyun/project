"""Reciprocal Rank Fusion over the lexical and vector methods.

    RRF(d) = sum_i 1 / (k + rank_i(d))

``k`` is fixed for every query and every combination (요청 section 19); nothing
is tuned per query.
"""

from __future__ import annotations

from lexical_search import ScoredDocument

#: Standard RRF constant from the original paper. Fixed, never tuned per query.
RRF_K = 60


def reciprocal_rank_fusion(
    rankings: list[list[ScoredDocument]], k: int = RRF_K, top_k: int = 10
) -> list[ScoredDocument]:
    """Fuse ranked lists by rank position only.

    Scores from the input methods are deliberately ignored: they are on
    incomparable scales (ts_rank_cd, trigram similarity, cosine), and rank
    fusion is what makes them combinable without calibration.

    Ties are broken by document_id so the output is deterministic.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for position, scored in enumerate(ranking, start=1):
            fused[scored.document_id] = fused.get(scored.document_id, 0.0) + 1.0 / (k + position)
    ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    return [ScoredDocument(doc_id, score) for doc_id, score in ordered[:top_k]]


class HybridSearch:
    """Runs the component methods and fuses their rankings."""

    def __init__(self, name: str, methods: list, k: int = RRF_K, candidate_k: int = 10):
        self.name = name
        self.methods = methods
        self.k = k
        # Fuse over a candidate pool at least as deep as the reported top_k so
        # a document ranked 8th by one method can still surface.
        self.candidate_k = candidate_k

    def search(self, query: str, top_k: int = 10) -> list[ScoredDocument]:
        rankings = [m.search(query, max(self.candidate_k, top_k)) for m in self.methods]
        return reciprocal_rank_fusion(rankings, k=self.k, top_k=top_k)
