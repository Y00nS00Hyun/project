"""Evaluation harness for the Korean search PoC.

Runs every search method over the same dataset and reports Recall@1, Recall@5,
MRR and nDCG@5, overall and per query category.

The harness is dataset-agnostic: point it at a different documents.jsonl /
queries.jsonl and the same comparison runs on real data (요청 section 13).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataset import Dataset, Query, chunk_document, load_dataset  # noqa: E402
from lexical_search import ScoredDocument  # noqa: E402

TOP_K = 10

#: Graded relevance used by nDCG: the primary document is worth more than a
#: merely relevant one, because ranking the right year/department first is the
#: thing these queries are actually testing.
GAIN_PRIMARY = 2.0
GAIN_RELEVANT = 1.0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return float("nan")
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for position, doc_id in enumerate(ranked, start=1):
        if doc_id in relevant:
            return 1.0 / position
    return 0.0


def primary_rank(ranked: list[str], primary: str | None) -> int | None:
    if primary is None:
        return None
    for position, doc_id in enumerate(ranked, start=1):
        if doc_id == primary:
            return position
    return None


def ndcg_at_k(ranked: list[str], query: Query, k: int) -> float:
    if not query.relevant_documents:
        return float("nan")

    def gain(doc_id: str) -> float:
        if doc_id == query.primary_document:
            return GAIN_PRIMARY
        return GAIN_RELEVANT if doc_id in query.relevant_documents else 0.0

    dcg = sum(gain(d) / math.log2(i + 1) for i, d in enumerate(ranked[:k], start=1))
    ideal = sorted((gain(d) for d in query.relevant_documents), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal, start=1))
    return dcg / idcg if idcg else float("nan")


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    query_id: str
    query: str
    category: str
    difficulty: str
    method: str
    ranked_documents: list[str]
    scores: list[float]
    relevant_documents: list[str]
    primary_document: str | None
    recall_at_1: float
    recall_at_5: float
    reciprocal_rank: float
    ndcg_at_5: float
    primary_rank: int | None
    top_score: float | None
    latency_ms: float


@dataclass
class MethodMetrics:
    method: str
    answerable_queries: int = 0
    recall_at_1: float = 0.0
    recall_at_5: float = 0.0
    mrr: float = 0.0
    ndcg_at_5: float = 0.0
    primary_at_1: float = 0.0
    primary_at_5: float = 0.0
    latency_ms_mean: float = 0.0
    latency_ms_p50: float = 0.0
    latency_ms_p95: float = 0.0
    no_answer_top_score_mean: float | None = None
    answerable_top_score_mean: float | None = None
    per_category: dict[str, dict[str, float]] = field(default_factory=dict)


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 4) if values else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(pct / 100.0 * len(ordered) + 0.5))))
    return round(ordered[rank - 1], 3)


def evaluate_method(method, dataset: Dataset, top_k: int = TOP_K) -> tuple[MethodMetrics, list[QueryResult]]:
    results: list[QueryResult] = []

    for query in dataset.queries:
        started = time.perf_counter()
        scored: list[ScoredDocument] = method.search(query.query, top_k)
        latency = (time.perf_counter() - started) * 1000.0

        ranked = [s.document_id for s in scored]
        relevant = set(query.relevant_documents)
        results.append(
            QueryResult(
                query_id=query.query_id,
                query=query.query,
                category=query.category,
                difficulty=query.difficulty,
                method=method.name,
                ranked_documents=ranked,
                scores=[round(s.score, 6) for s in scored],
                relevant_documents=list(query.relevant_documents),
                primary_document=query.primary_document,
                recall_at_1=recall_at_k(ranked, relevant, 1),
                recall_at_5=recall_at_k(ranked, relevant, 5),
                reciprocal_rank=reciprocal_rank(ranked, relevant) if relevant else float("nan"),
                ndcg_at_5=ndcg_at_k(ranked, query, 5),
                primary_rank=primary_rank(ranked, query.primary_document),
                top_score=round(scored[0].score, 6) if scored else None,
                latency_ms=round(latency, 3),
            )
        )

    answerable = [r for r in results if r.relevant_documents]
    no_answer = [r for r in results if not r.relevant_documents]

    metrics = MethodMetrics(
        method=method.name,
        answerable_queries=len(answerable),
        recall_at_1=_mean([r.recall_at_1 for r in answerable]),
        recall_at_5=_mean([r.recall_at_5 for r in answerable]),
        mrr=_mean([r.reciprocal_rank for r in answerable]),
        ndcg_at_5=_mean([r.ndcg_at_5 for r in answerable]),
        primary_at_1=_mean([1.0 if r.primary_rank == 1 else 0.0 for r in answerable]),
        primary_at_5=_mean(
            [1.0 if (r.primary_rank is not None and r.primary_rank <= 5) else 0.0 for r in answerable]
        ),
        latency_ms_mean=_mean([r.latency_ms for r in results]),
        latency_ms_p50=_percentile([r.latency_ms for r in results], 50),
        latency_ms_p95=_percentile([r.latency_ms for r in results], 95),
        # No-answer queries are not scored for recall; what matters is whether
        # the top score looks different from a real hit (요청 section 10.12).
        no_answer_top_score_mean=_mean([r.top_score for r in no_answer if r.top_score is not None])
        if no_answer else None,
        answerable_top_score_mean=_mean([r.top_score for r in answerable if r.top_score is not None])
        if answerable else None,
    )

    categories = sorted({r.category for r in answerable})
    for category in categories:
        subset = [r for r in answerable if r.category == category]
        metrics.per_category[category] = {
            "queries": len(subset),
            "recall_at_1": _mean([r.recall_at_1 for r in subset]),
            "recall_at_5": _mean([r.recall_at_5 for r in subset]),
            "mrr": _mean([r.reciprocal_rank for r in subset]),
            "ndcg_at_5": _mean([r.ndcg_at_5 for r in subset]),
            "primary_at_1": _mean([1.0 if r.primary_rank == 1 else 0.0 for r in subset]),
        }
    return metrics, results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_metrics_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_category_csv(path: Path, all_metrics: list[MethodMetrics]) -> None:
    categories = sorted({c for m in all_metrics for c in m.per_category})
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["category", "metric"] + [m.method for m in all_metrics])
        for category in categories:
            for metric in ("queries", "recall_at_1", "recall_at_5", "mrr", "ndcg_at_5", "primary_at_1"):
                writer.writerow(
                    [category, metric]
                    + [m.per_category.get(category, {}).get(metric, "") for m in all_metrics]
                )


def write_query_results(path: Path, results: list[QueryResult]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for r in results:
            row = r.__dict__.copy()
            for key in ("recall_at_1", "recall_at_5", "reciprocal_rank", "ndcg_at_5"):
                if isinstance(row[key], float) and math.isnan(row[key]):
                    row[key] = None
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def print_table(all_metrics: list[MethodMetrics]) -> None:
    print("\n" + "=" * 92)
    print("OVERALL (answerable queries only)")
    print("-" * 92)
    header = f"{'Method':<22}{'R@1':>8}{'R@5':>8}{'MRR':>8}{'nDCG@5':>9}{'P@1':>8}{'P@5':>8}{'p50 ms':>9}{'p95 ms':>9}"
    print(header)
    for m in all_metrics:
        print(
            f"{m.method:<22}{m.recall_at_1:>8.3f}{m.recall_at_5:>8.3f}{m.mrr:>8.3f}"
            f"{m.ndcg_at_5:>9.3f}{m.primary_at_1:>8.3f}{m.primary_at_5:>8.3f}"
            f"{m.latency_ms_p50:>9.1f}{m.latency_ms_p95:>9.1f}"
        )
    print("-" * 92)
    print("P@1 / P@5 = primary_document at rank 1 / within top 5")
    print("=" * 92)


def print_category_table(all_metrics: list[MethodMetrics], metric: str = "recall_at_5") -> None:
    categories = sorted({c for m in all_metrics for c in m.per_category})
    print(f"\nBY CATEGORY ({metric})")
    print("-" * 92)
    print(f"{'Category':<20}{'n':>4}" + "".join(f"{m.method:>17}" for m in all_metrics))
    for category in categories:
        n = next((m.per_category[category]["queries"] for m in all_metrics if category in m.per_category), 0)
        row = f"{category:<20}{int(n):>4}"
        for m in all_metrics:
            value = m.per_category.get(category, {}).get(metric)
            row += f"{value:>17.3f}" if isinstance(value, (int, float)) else f"{'-':>17}"
        print(row)
    print("-" * 92)


# ---------------------------------------------------------------------------

def build_methods(conn, dataset: Dataset, use_vector: bool):
    from hybrid_search import HybridSearch
    from lexical_search import SimpleFTS, SimpleFTSOr, TrigramSearch

    fts = SimpleFTS(conn)
    fts_or = SimpleFTSOr(conn)
    trigram = TrigramSearch(conn)
    methods = [fts, fts_or, trigram]

    if use_vector:
        from vector_search import EmbeddingModel, VectorSearch

        vector = VectorSearch(conn, EmbeddingModel())
        methods.append(vector)
        methods.append(HybridSearch("rrf_fts_vector", [fts, vector]))
        methods.append(HybridSearch("rrf_fts_trgm_vector", [fts, trigram, vector]))
    methods.append(HybridSearch("rrf_fts_trgm", [fts, trigram]))
    return methods


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--documents", type=Path, default=None)
    ap.add_argument("--queries", type=Path, default=None)
    ap.add_argument("--output", type=Path, default=Path("artifacts/search-poc"))
    ap.add_argument("--no-vector", action="store_true", help="skip embedding-based methods")
    ap.add_argument("--top-k", type=int, default=TOP_K)
    args = ap.parse_args(argv)

    import db
    from vector_search import DEFAULT_MODEL

    dataset = load_dataset(args.documents, args.queries)
    problems = dataset.validate()
    if problems:
        print("Dataset label problems:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 2

    chunks = [c for d in dataset.documents for c in chunk_document(d)]
    print(f"Dataset : {len(dataset.documents)} documents, {len(chunks)} chunks, "
          f"{len(dataset.queries)} queries")

    server = db.start_server(db.default_data_dir())
    conn = db.connect(server.get_uri())

    embeddings = None
    model_name = None
    if not args.no_vector:
        from vector_search import EmbeddingModel, embed_chunks

        print(f"Embedding model: {DEFAULT_MODEL} (PoC only, not a production decision)")
        model = EmbeddingModel()
        started = time.perf_counter()
        embeddings = embed_chunks(model, chunks)
        print(f"Embedded {len(embeddings)} chunks in {time.perf_counter() - started:.1f}s")
        model_name = DEFAULT_MODEL

    db.load_corpus(conn, dataset.documents, chunks, embeddings)

    methods = build_methods(conn, dataset, use_vector=not args.no_vector)

    all_metrics: list[MethodMetrics] = []
    all_results: list[QueryResult] = []
    for method in methods:
        metrics, results = evaluate_method(method, dataset, args.top_k)
        all_metrics.append(metrics)
        all_results.extend(results)

    print_table(all_metrics)
    print_category_table(all_metrics, "recall_at_5")
    print_category_table(all_metrics, "primary_at_1")

    args.output.mkdir(parents=True, exist_ok=True)
    write_metrics_json(
        args.output / "metrics.json",
        {
            "dataset": {
                "documents": len(dataset.documents),
                "chunks": len(chunks),
                "queries": len(dataset.queries),
                "answerable_queries": sum(1 for q in dataset.queries if q.is_answerable),
                "categories": sorted({q.category for q in dataset.queries}),
                "documents_path": str(args.documents or "poc/search/fixtures/documents.jsonl"),
                "queries_path": str(args.queries or "poc/search/fixtures/queries.jsonl"),
            },
            "configuration": {
                "top_k": args.top_k,
                "embedding_model": model_name,
                "embedding_note": "PoC only; production model not decided",
                "rrf_k": __import__("hybrid_search").RRF_K,
                "trigram_floor": __import__("lexical_search").TrigramSearch.SIMILARITY_FLOOR,
                "fts_configuration": "simple",
                "chunking": "title + sentence baseline; not a chunking decision",
                "document_aggregation": "best matching chunk",
            },
            "methods": [m.__dict__ for m in all_metrics],
        },
    )
    write_category_csv(args.output / "metrics_by_category.csv", all_metrics)
    write_query_results(args.output / "query_results.jsonl", all_results)
    print(f"\nWrote {args.output}/metrics.json, metrics_by_category.csv, query_results.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
