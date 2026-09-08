"""Bounded Chunking + Embedding PoC, reusing the existing search harness.

Nine vector configurations, two sizes, two models, no overlap tuning. The
runner uses isolated CPU model workers, then reuses pgvector document-max
retrieval, metrics, pg_trgm, and RRF. Binary intermediates stay temporary.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from chunkers import chunk_blocks, count_tokens
from dataset import Dataset, FIXTURES, load_dataset
from embedding_models import MODELS, LocalEmbeddingModel
from evaluate import evaluate_method, write_query_results
from hybrid_search import HybridSearch, RRF_K
from lexical_search import TrigramSearch
from structured_dataset import LAYOUT, load_layout, structure_document
from vector_search import VectorSearch

ROOT = Path(__file__).resolve().parents[2]
STRATEGIES = ("fixed", "paragraph", "table")
CONFIGURATIONS = [
    {"id": f"C{i + 1}", "strategy": strategy, "size": 64, "overlap": 0, "model": model}
    for i, (model, strategy) in enumerate((m, s) for m in MODELS for s in STRATEGIES)
] + [
    {"id": f"C{i + 7}", "strategy": strategy, "size": 128, "overlap": 0, "model": "e5-small"}
    for i, strategy in enumerate(STRATEGIES)
]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        raise ValueError("cannot write an empty metrics table")
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def percentile(values, p):
    return float(np.percentile(values, p))


def chunk_statistics(config, chunks, document_count):
    tokens = [c.token_count for c in chunks]
    return {"configuration": config["id"], "strategy": config["strategy"],
            "size": config["size"], "model": config["model"], "chunk_count": len(chunks),
            "chunks_per_document_mean": len(chunks) / document_count,
            "tokens_mean": statistics.fmean(tokens), "tokens_p50": percentile(tokens, 50),
            "tokens_p95": percentile(tokens, 95), "tokens_min": min(tokens), "tokens_max": max(tokens),
            "table_chunks": sum(c.content_type == "TABLE" for c in chunks),
            "tiny_chunks_lt_16": sum(n < 16 for n in tokens),
            "oversize_chunks": sum(n > config["size"] for n in tokens)}


def worker(args):
    dataset = load_dataset(args.documents, args.queries)
    layouts = load_layout(args.layout)
    model = LocalEmbeddingModel(MODELS[args.worker], str(args.cache_dir), args.threads)
    model.encode(["준비"], model.spec.query_prefix, 1)  # unmeasured warm-up
    query_vectors = None
    timings = []
    # Single-query, three full passes, fixed order; includes no_answer queries.
    for repeat in range(3):
        encoded = []
        for q in dataset.queries:
            started = time.perf_counter()
            encoded.append(model.encode([q.query], model.spec.query_prefix, 1)[0])
            timings.append({"repeat": repeat, "query_id": q.query_id,
                            "milliseconds": (time.perf_counter() - started) * 1000})
        if query_vectors is None:
            query_vectors = np.array(encoded)
    output = args.worker_output
    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "queries.npy", query_vectors)
    entries, stats, document_lengths = [], [], []
    structured = {d.document_id: structure_document(d, layouts) for d in dataset.documents}
    for d in dataset.documents:
        document_lengths.append({"document_id": d.document_id,
                                 "original_searchable_tokens": count_tokens(model.tokenizer, d.searchable_content),
                                 "structured_tokens": count_tokens(model.tokenizer, "\n\n".join(b.text for b in structured[d.document_id]))})
    for config in CONFIGURATIONS:
        if config["model"] != args.worker:
            continue
        chunks = [c for d in dataset.documents for c in chunk_blocks(
            d.document_id, structured[d.document_id], model.tokenizer, config["strategy"], config["size"])]
        # Warm passage kernels outside the timed full-corpus pass.
        model.encode([chunks[0].text], model.spec.document_prefix, 1)
        start = time.perf_counter()
        vectors = model.encode([c.text for c in chunks], model.spec.document_prefix, args.batch_size)
        seconds = time.perf_counter() - start
        np.save(output / f'{config["id"]}.npy', vectors)
        write_json(output / f'{config["id"]}.chunks.json', [asdict(c) for c in chunks])
        entries.append({"configuration": config["id"], "corpus_embedding_seconds": seconds,
                        "chunk_embedding_ms_mean": seconds * 1000 / len(chunks),
                        "document_embedding_ms_mean": seconds * 1000 / len(dataset.documents),
                        "chunk_count": len(chunks)})
        stats.append(chunk_statistics(config, chunks, len(dataset.documents)))
        print(f'{config["id"]}: {len(chunks)} chunks, embedding {seconds:.3f}s', flush=True)
    write_json(output / "worker.json", {
        "model": model.metadata(), "corpus_timings": entries, "chunk_statistics": stats,
        "query_timings": timings, "document_lengths": document_lengths,
        "query_embedding_ms_p50": percentile([t["milliseconds"] for t in timings], 50),
        "query_embedding_ms_p95": percentile([t["milliseconds"] for t in timings], 95),
        "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "peak_memory_scope": "Linux process high-water RSS, imports + one model + all its configurations; not incremental model RAM",
        "tokenizer_sha256": hashlib.sha256(model.tokenizer.backend_tokenizer.to_str().encode()).hexdigest(),
    })


class CachedQueries:
    """Use measured offline embeddings with the EXISTING pgvector search class."""
    def __init__(self, queries, vectors):
        self.by_query = {q.query: v.tolist() for q, v in zip(queries, vectors, strict=True)}

    def embed_query(self, query):
        return self.by_query[query]


def selection_key(metrics, configuration):
    """Fixed quality order then smaller model, paragraph simplicity, smaller size.

    No per-query tuning. P@1 here retains the legacy primary-at-1 meaning.
    """
    return (metrics.primary_at_1, metrics.recall_at_5, metrics.ndcg_at_5, metrics.mrr,
            -MODELS[configuration["model"]].dimension,
            {"paragraph": 2, "fixed": 1, "table": 0}[configuration["strategy"]],
            -configuration["size"])


def category_rows(metrics, results):
    rows = [{"configuration": metrics.method, "category": category,
             **values, "top_score_mean": None} for category, values in metrics.per_category.items()]
    absent = [r for r in results if not r.relevant_documents]
    if absent:
        rows.append({"configuration": metrics.method, "category": "no_answer", "queries": len(absent),
                     "recall_at_1": None, "recall_at_5": None, "mrr": None, "ndcg_at_5": None,
                     "primary_at_1": None, "top_score_mean": statistics.fmean(r.top_score for r in absent)})
    return rows


def flat_metrics(metrics):
    return {k: v for k, v in asdict(metrics).items() if k != "per_category"}


def diagnostics(config, chunks, vectors, query_vectors, dataset):
    """Chunk crowding BEFORE document dedup; all ranked documents use DB max."""
    rows = []
    for q, vector in zip(dataset.queries, query_vectors, strict=True):
        scores = vectors @ vector
        order = sorted(range(len(chunks)), key=lambda i: (-float(scores[i]), chunks[i].document_id, chunks[i].chunk_index))[:5]
        counts = Counter(chunks[i].document_id for i in order)
        rows.append({"configuration": config["id"], "query_id": q.query_id,
                     "unique_documents_raw_top5": len(counts),
                     "max_chunks_one_document_raw_top5": max(counts.values()),
                     "top_chunks": [{"document_id": chunks[i].document_id,
                                     "chunk_index": chunks[i].chunk_index,
                                     "score": float(scores[i])} for i in order]})
    return rows


def run(args):
    import db
    from chunkers import Chunk

    dataset = load_dataset(args.documents, args.queries)
    problems = dataset.validate()
    if problems or not dataset.documents or not dataset.queries:
        raise ValueError(problems or "dataset is empty")
    if len({q.query for q in dataset.queries}) != len(dataset.queries):
        raise ValueError("query text must be unique for cached encoding")
    args.output.mkdir(parents=True, exist_ok=True)
    inputs = {str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p): digest(p) for p in
              (args.documents, args.queries, args.layout, ROOT / "docs/functional-spec-v2.3.md",
               ROOT / "docs/database-schema-v2.3.md")}
    run_info = {"status": "RUNNING", "started_utc": datetime.now(timezone.utc).isoformat(),
                "configurations": CONFIGURATIONS, "models": [asdict(m) for m in MODELS.values()],
                "input_sha256": inputs, "documents": len(dataset.documents), "queries": len(dataset.queries),
                "answerable_queries": sum(q.is_answerable for q in dataset.queries),
                "sizes": [64, 128], "overlap": 0, "batch_size": args.batch_size, "threads": args.threads,
                "device": "cpu", "python": platform.python_version(), "platform": platform.platform(),
                "cpu": next((s.split(':', 1)[1].strip() for s in Path('/proc/cpuinfo').read_text().splitlines() if s.startswith('model name')), platform.processor()),
                "visible_cpu_count": os.cpu_count(),
                "packages": {p: importlib.metadata.version(p) for p in
                             ("torch", "transformers", "sentence-transformers", "tokenizers", "numpy", "psycopg", "pgserver")},
                "retrieval": "exact pgvector cosine, max chunk score per document, ties document_id ASC, top10",
                "rrf_k": RRF_K, "rrf_candidate_k": 10, "trigram_floor": TrigramSearch.SIMILARITY_FLOOR,
                "selection_policy": "primary_at_1, recall_at_5, ndcg_at_5, mrr; tie: lower dimension, paragraph > fixed > table, smaller size",
                "latency_note": "evaluation latency excludes cached query encoding; separate latency.csv contains measured encoding",
                "token_percentiles": "numpy linear percentiles; legacy retrieval-latency percentiles unchanged",
                "source_sha256": {str(p.relative_to(ROOT)): digest(p) for p in sorted(Path(__file__).parent.glob('*.py'))}}
    write_json(args.output / "configurations.json", run_info)
    with tempfile.TemporaryDirectory(prefix="chunking-embedding-") as temp:
        working = Path(temp)
        worker_results = {}
        for model in MODELS:
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", model,
                       "--worker-output", str(working / model), "--cache-dir", str(args.cache_dir),
                       "--documents", str(args.documents), "--queries", str(args.queries),
                       "--layout", str(args.layout), "--threads", str(args.threads), "--batch-size", str(args.batch_size)]
            subprocess.run(command, check=True, env={**os.environ, "HF_HUB_OFFLINE": "1",
                                                    "TOKENIZERS_PARALLELISM": "false"})
            worker_results[model] = json.loads((working / model / "worker.json").read_text())
        # Model releases serialize tokenizers differently. Verify actual chunk
        # text, token counts and anchors rather than require byte-identical JSON.
        for small, base in zip(("C1", "C2", "C3"), ("C4", "C5", "C6"), strict=True):
            if digest(working / "e5-small" / f"{small}.chunks.json") != digest(working / "e5-base" / f"{base}.chunks.json"):
                raise ValueError("candidate tokenization changed chunk text/counts/anchors; matched comparison invalid")
        run_info["matched_model_chunks_verified"] = True
        # Private throwaway cluster: never connect to any existing production DB.
        server = db.start_server(working / "pgdata", cleanup_mode="stop")
        conn = db.connect(server.get_uri())
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                run_info["postgresql"] = cur.fetchone()[0]
                cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
                run_info["extensions"] = dict(cur.fetchall())
            all_metrics, all_results, all_categories, all_chunks, all_diagnostics = [], [], [], [], []
            scores_by_config = {}

            def load_config(config):
                location = working / config["model"]
                chunks = [Chunk(**c) for c in json.loads((location / f'{config["id"]}.chunks.json').read_text())]
                embeddings = np.load(location / f'{config["id"]}.npy')
                query_vectors = np.load(location / "queries.npy")
                db.load_corpus(conn, dataset.documents, chunks,
                               {(c.document_id, c.chunk_index): v.tolist() for c, v in zip(chunks, embeddings, strict=True)},
                               embedding_dim=MODELS[config["model"]].dimension)
                method = VectorSearch(conn, CachedQueries(dataset.queries, query_vectors))
                method.name = config["id"]
                return method, chunks, embeddings, query_vectors

            table_query_ids = set(json.loads(args.layout.read_text())["table_queries"])
            table_dataset = Dataset(dataset.documents, [q for q in dataset.queries if q.query_id in table_query_ids])
            table_metrics = []
            for config in CONFIGURATIONS:
                method, chunks, embeddings, query_vectors = load_config(config)
                metrics, results = evaluate_method(method, dataset)
                all_metrics.append(flat_metrics(metrics))
                all_results.extend(results)
                all_categories.extend(category_rows(metrics, results))
                scores_by_config[config["id"]] = metrics
                all_chunks.extend({"configuration": config["id"], **asdict(c)} for c in chunks)
                all_diagnostics.extend(diagnostics(config, chunks, embeddings, query_vectors, dataset))
                if table_dataset.queries:
                    tm, _ = evaluate_method(method, table_dataset)
                    table_metrics.append(flat_metrics(tm))
                print(f'{config["id"]}: R@1={metrics.recall_at_1:.4f} R@5={metrics.recall_at_5:.4f} P@1={metrics.primary_at_1:.4f}', flush=True)
            best = max(CONFIGURATIONS, key=lambda c: selection_key(scores_by_config[c["id"]], c))
            vector, _, _, _ = load_config(best)
            hybrid = HybridSearch("best_vector_trgm_rrf", [TrigramSearch(conn), vector], k=RRF_K, candidate_k=10)
            hm, hr = evaluate_method(hybrid, dataset)
            all_metrics.append(flat_metrics(hm))
            all_results.extend(hr)
            all_categories.extend(category_rows(hm, hr))
            if table_dataset.queries:
                tm, _ = evaluate_method(hybrid, table_dataset)
                table_metrics.append(flat_metrics(tm))
        finally:
            conn.close()
            server.cleanup()
        run_info.update(status="COMPLETE", best_configuration=best,
                        finished_utc=datetime.now(timezone.utc).isoformat())
        write_csv(args.output / "metrics.csv", all_metrics)
        write_csv(args.output / "category_metrics.csv", all_categories)
        write_csv(args.output / "table_metrics.csv", table_metrics)
        write_csv(args.output / "chunk_statistics.csv", [s for w in worker_results.values() for s in w["chunk_statistics"]])
        latency = []
        for key, w in worker_results.items():
            for entry in w["corpus_timings"]:
                latency.append({**entry, "model": key, "dimension": w["model"]["dimension"],
                                "model_load_seconds": w["model"]["load_seconds"],
                                "query_embedding_ms_p50": w["query_embedding_ms_p50"],
                                "query_embedding_ms_p95": w["query_embedding_ms_p95"],
                                "peak_process_rss_mib": w["peak_process_rss_mib"]})
        write_csv(args.output / "latency.csv", latency)
        write_query_results(args.output / "query_results.jsonl", all_results)
        for filename, rows in (("chunks.jsonl", all_chunks), ("chunk_retrieval.jsonl", all_diagnostics)):
            with (args.output / filename).open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        write_json(args.output / "model_measurements.json", worker_results)
        # Check controlled inputs, including the authoritative specifications.
        if any(digest(ROOT / path) != checksum for path, checksum in inputs.items()):
            raise ValueError("input or authoritative specification changed during evaluation")
        write_json(args.output / "configurations.json", run_info)
        print("Best configuration:", best, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--documents", type=Path, default=FIXTURES / "documents.jsonl")
    parser.add_argument("--queries", type=Path, default=FIXTURES / "queries.jsonl")
    parser.add_argument("--layout", type=Path, default=LAYOUT)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/chunking-embedding-poc")
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/chunking-embedding-hf/hub"))
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--worker", choices=list(MODELS))
    parser.add_argument("--worker-output", type=Path)
    args = parser.parse_args()
    if args.threads < 1 or args.batch_size < 1:
        parser.error("threads and batch-size must be positive")
    if args.worker:
        if args.worker_output is None:
            parser.error("worker-output required")
        worker(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
