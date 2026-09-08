"""Unit tests for the Korean search PoC harness.

These cover the dataset contract, the fusion and the metrics -- everything that
decides what the comparison *says*. They deliberately need neither PostgreSQL
nor the embedding model, so a mistake in the scoring maths is caught without a
20-minute run.
"""

from __future__ import annotations

import json
import math

import pytest

from dataset import (
    Dataset,
    Document,
    Query,
    chunk_document,
    load_dataset,
)
from evaluate import (
    evaluate_method,
    ndcg_at_k,
    primary_rank,
    recall_at_k,
    reciprocal_rank,
)
from hybrid_search import RRF_K, HybridSearch, reciprocal_rank_fusion
from lexical_search import ScoredDocument


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def doc(doc_id: str, title: str = "제목", text: str = "본문이다.") -> Document:
    return Document(doc_id, title, "기획조정실", 2026, "보고서", text)


def query(rel, primary, category="keyword") -> Query:
    return Query("Q999", "질의", tuple(rel), primary, category, "medium")


class StubMethod:
    """A search method with a fixed answer, for testing the harness itself."""

    def __init__(self, name, ranking_by_query):
        self.name = name
        self.ranking_by_query = ranking_by_query
        self.calls = 0

    def search(self, q, top_k=10):
        self.calls += 1
        return [ScoredDocument(d, s) for d, s in self.ranking_by_query.get(q, [])][:top_k]


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class TestDatasetLoading:
    def test_shipped_dataset_loads_and_validates(self):
        ds = load_dataset()
        assert ds.validate() == []
        assert len(ds.documents) >= 15
        assert 20 <= len(ds.queries) <= 30

    def test_shipped_dataset_covers_the_required_failure_modes(self):
        ds = load_dataset()
        categories = {q.category for q in ds.queries}
        required = {
            "exact_title", "keyword", "spacing", "typo", "partial_match",
            "morphology", "semantic", "year", "department", "numeric",
            "similar_document", "no_answer",
        }
        assert required <= categories

    def test_searchable_content_includes_metadata(self):
        d = doc("DOC-001", title="2026년 예산안", text="본문")
        content = d.searchable_content
        assert "2026년 예산안" in content and "기획조정실" in content and "본문" in content

    def test_no_answer_queries_have_no_primary(self):
        ds = load_dataset()
        for q in ds.queries:
            if q.category == "no_answer":
                assert q.relevant_documents == ()
                assert q.primary_document is None
                assert not q.is_answerable

    def test_documents_and_queries_are_swappable(self, tmp_path):
        # The whole point of the file-based dataset: point it elsewhere and the
        # same harness runs on real data.
        docs = tmp_path / "d.jsonl"
        qs = tmp_path / "q.jsonl"
        docs.write_text(json.dumps({
            "document_id": "X-1", "title": "t", "department": "d",
            "year": 2026, "document_type": "보고서", "text": "본문",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        qs.write_text(json.dumps({
            "query_id": "Q1", "query": "t", "relevant_documents": ["X-1"],
            "primary_document": "X-1", "category": "exact_title", "difficulty": "easy",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        ds = load_dataset(docs, qs)
        assert ds.validate() == []
        assert ds.document_ids == {"X-1"}


class TestDatasetValidation:
    def test_unknown_relevant_document_is_reported(self):
        ds = Dataset([doc("DOC-001")], [query(["DOC-999"], "DOC-999")])
        assert any("unknown relevant document" in p for p in ds.validate())

    def test_primary_outside_relevant_is_reported(self):
        ds = Dataset([doc("DOC-001"), doc("DOC-002")], [query(["DOC-001"], "DOC-002")])
        assert any("primary not in relevant" in p for p in ds.validate())

    def test_answerable_without_primary_is_reported(self):
        ds = Dataset([doc("DOC-001")], [query(["DOC-001"], None)])
        assert any("without a primary" in p for p in ds.validate())

    def test_duplicate_document_id_is_reported(self):
        ds = Dataset([doc("DOC-001"), doc("DOC-001")], [])
        assert any("duplicate document_id" in p for p in ds.validate())


class TestChunking:
    def test_title_is_chunk_zero(self):
        chunks = chunk_document(doc("DOC-001", title="예산안", text="가나다. 라마바."))
        assert chunks[0].chunk_index == 0
        assert "예산안" in chunks[0].text

    def test_sentences_become_separate_chunks(self):
        chunks = chunk_document(doc("DOC-001", text="첫 문장이다. 둘째 문장이다. 셋째 문장이다."))
        assert len(chunks) == 4  # title + 3 sentences
        assert [c.chunk_index for c in chunks] == [0, 1, 2, 3]

    def test_every_chunk_keeps_its_document_id(self):
        chunks = chunk_document(doc("DOC-042", text="가. 나. 다."))
        assert {c.document_id for c in chunks} == {"DOC-042"}

    def test_chunking_is_deterministic(self):
        d = doc("DOC-001", text="가나다. 라마바. 사아자.")
        assert [c.text for c in chunk_document(d)] == [c.text for c in chunk_document(d)]


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------

class TestReciprocalRankFusion:
    def test_agreement_between_lists_beats_a_single_top_hit(self):
        a = [ScoredDocument("A", 9.0), ScoredDocument("B", 1.0)]
        b = [ScoredDocument("C", 9.0), ScoredDocument("B", 1.0)]
        fused = reciprocal_rank_fusion([a, b])
        # B is 2nd in both; A and C are 1st in one list only.
        assert fused[0].document_id == "B"

    def test_uses_rank_not_score(self):
        # Wildly different score scales must not change the outcome.
        a = [ScoredDocument("A", 1000.0), ScoredDocument("B", 999.0)]
        b = [ScoredDocument("A", 0.001), ScoredDocument("B", 0.0009)]
        assert [s.document_id for s in reciprocal_rank_fusion([a, b])] == ["A", "B"]

    def test_score_matches_the_formula(self):
        a = [ScoredDocument("A", 1.0)]
        b = [ScoredDocument("A", 1.0)]
        fused = reciprocal_rank_fusion([a, b], k=60)
        assert fused[0].score == pytest.approx(2 * (1 / 61))

    def test_ties_broken_deterministically_by_document_id(self):
        a = [ScoredDocument("B", 1.0)]
        b = [ScoredDocument("A", 1.0)]
        assert [s.document_id for s in reciprocal_rank_fusion([a, b])] == ["A", "B"]

    def test_empty_inputs_give_empty_output(self):
        assert reciprocal_rank_fusion([[], []]) == []

    def test_respects_top_k(self):
        ranking = [ScoredDocument(f"D{i}", 1.0) for i in range(10)]
        assert len(reciprocal_rank_fusion([ranking], top_k=3)) == 3

    def test_default_k_is_fixed(self):
        assert RRF_K == 60


class TestHybridSearch:
    def test_queries_every_component(self):
        m1 = StubMethod("m1", {"q": [("A", 1.0)]})
        m2 = StubMethod("m2", {"q": [("B", 1.0)]})
        hybrid = HybridSearch("h", [m1, m2])
        result = hybrid.search("q", top_k=5)
        assert m1.calls == 1 and m2.calls == 1
        assert {s.document_id for s in result} == {"A", "B"}

    def test_fuses_over_a_deep_candidate_pool(self):
        # A document ranked 8th by one method must still be reachable.
        deep = [("X%d" % i, 1.0) for i in range(10)]
        m1 = StubMethod("m1", {"q": deep})
        m2 = StubMethod("m2", {"q": [("X8", 1.0)]})
        hybrid = HybridSearch("h", [m1, m2], candidate_k=10)
        assert hybrid.search("q", top_k=3)[0].document_id == "X8"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestRecall:
    def test_perfect_recall(self):
        assert recall_at_k(["A", "B"], {"A", "B"}, 5) == 1.0

    def test_partial_recall(self):
        assert recall_at_k(["A", "C"], {"A", "B"}, 5) == 0.5

    def test_cutoff_is_respected(self):
        assert recall_at_k(["C", "A"], {"A"}, 1) == 0.0
        assert recall_at_k(["C", "A"], {"A"}, 2) == 1.0

    def test_no_relevant_documents_is_undefined(self):
        assert math.isnan(recall_at_k(["A"], set(), 5))

    def test_empty_ranking(self):
        assert recall_at_k([], {"A"}, 5) == 0.0


class TestReciprocalRank:
    def test_first_position(self):
        assert reciprocal_rank(["A", "B"], {"A"}) == 1.0

    def test_third_position(self):
        assert reciprocal_rank(["X", "Y", "A"], {"A"}) == pytest.approx(1 / 3)

    def test_uses_the_first_relevant_hit(self):
        assert reciprocal_rank(["X", "B", "A"], {"A", "B"}) == 0.5

    def test_miss_is_zero(self):
        assert reciprocal_rank(["X", "Y"], {"A"}) == 0.0


class TestPrimaryRank:
    def test_found(self):
        assert primary_rank(["X", "A"], "A") == 2

    def test_missing(self):
        assert primary_rank(["X"], "A") is None

    def test_no_primary(self):
        assert primary_rank(["X"], None) is None


class TestNdcg:
    def test_ideal_ordering_scores_one(self):
        q = query(["A", "B"], "A")
        assert ndcg_at_k(["A", "B"], q, 5) == pytest.approx(1.0)

    def test_primary_below_a_secondary_scores_less_than_one(self):
        q = query(["A", "B"], "A")
        assert ndcg_at_k(["B", "A"], q, 5) < 1.0

    def test_irrelevant_results_score_zero(self):
        q = query(["A"], "A")
        assert ndcg_at_k(["X", "Y"], q, 5) == 0.0

    def test_undefined_without_relevant_documents(self):
        assert math.isnan(ndcg_at_k(["A"], query([], None), 5))

    def test_primary_outranks_a_merely_relevant_document(self):
        q = query(["A", "B"], "A")
        primary_first = ndcg_at_k(["A", "X"], q, 5)
        relevant_first = ndcg_at_k(["B", "X"], q, 5)
        assert primary_first > relevant_first


# ---------------------------------------------------------------------------
# Harness behaviour
# ---------------------------------------------------------------------------

class TestEvaluateMethod:
    def make_dataset(self) -> Dataset:
        return Dataset(
            documents=[doc("DOC-001"), doc("DOC-002")],
            queries=[
                Query("Q1", "a", ("DOC-001",), "DOC-001", "exact_title", "easy"),
                Query("Q2", "b", ("DOC-002",), "DOC-002", "semantic", "hard"),
                Query("Q3", "c", (), None, "no_answer", "hard"),
            ],
        )

    def test_metrics_are_computed_over_answerable_queries_only(self):
        method = StubMethod("stub", {"a": [("DOC-001", 1.0)], "b": [("DOC-001", 1.0)], "c": [("DOC-001", 1.0)]})
        metrics, results = evaluate_method(method, self.make_dataset())
        assert metrics.answerable_queries == 2
        assert metrics.recall_at_1 == 0.5      # Q1 hit, Q2 miss; Q3 excluded
        assert len(results) == 3

    def test_no_answer_scores_are_tracked_separately(self):
        method = StubMethod("stub", {"a": [("DOC-001", 0.9)], "b": [("DOC-002", 0.9)], "c": [("DOC-001", 0.8)]})
        metrics, _ = evaluate_method(method, self.make_dataset())
        assert metrics.no_answer_top_score_mean == pytest.approx(0.8)
        assert metrics.answerable_top_score_mean == pytest.approx(0.9)

    def test_per_category_breakdown_is_produced(self):
        method = StubMethod("stub", {"a": [("DOC-001", 1.0)], "b": [], "c": []})
        metrics, _ = evaluate_method(method, self.make_dataset())
        assert metrics.per_category["exact_title"]["recall_at_5"] == 1.0
        assert metrics.per_category["semantic"]["recall_at_5"] == 0.0
        assert "no_answer" not in metrics.per_category

    def test_evaluation_is_deterministic(self):
        method = StubMethod("stub", {"a": [("DOC-001", 1.0)], "b": [("DOC-002", 1.0)], "c": []})
        first, first_rows = evaluate_method(method, self.make_dataset())
        second, second_rows = evaluate_method(method, self.make_dataset())
        assert first.recall_at_1 == second.recall_at_1
        assert first.mrr == second.mrr
        assert first.ndcg_at_5 == second.ndcg_at_5
        assert [r.ranked_documents for r in first_rows] == [r.ranked_documents for r in second_rows]

    def test_empty_result_list_does_not_crash(self):
        method = StubMethod("stub", {})
        metrics, results = evaluate_method(method, self.make_dataset())
        assert metrics.recall_at_1 == 0.0
        assert all(r.top_score is None for r in results)
