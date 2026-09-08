#!/usr/bin/env python3
"""Measure the HWP/HWPX parser against an internal document corpus.

    python scripts/run_internal_corpus.py \
        --input /internal/share/sample-corpus \
        --output artifacts/internal-hwp-poc

Run this **on the internal VM**. It never sends anything anywhere: it reads
documents locally and writes counts, hashes and codes to --output.

What the output does NOT contain, by construction:

    filenames, paths, document text, or any per-document identifier other than
    a generated HWP-001 / HWPX-001 label.

Use --mapping-file only if you need to trace a finding back to a real document.
That file contains real paths; keep it on the internal VM and do not commit it
(.gitignore already excludes *.corpus-mapping.json).
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_processing import __version__  # noqa: E402
from document_processing.corpus import (  # noqa: E402
    CorpusRecord,
    assign_anonymous_ids,
    container_mismatches,
    detect_container,
    extension_matches_container,
    failure_buckets,
    format_summary,
    memory_ratio_by_format,
    peak_rss_by_format,
    parse_status_for,
    performance_by_bucket,
    result_matrix,
)

SUPPORTED = {".hwp", ".hwpx"}
WORKER = "document_processing.corpus_worker"


# ---------------------------------------------------------------------------
# Running one document
# ---------------------------------------------------------------------------

def run_worker(path: Path, timeout: float) -> tuple[dict, bool]:
    """Parse ``path`` in a child process. Returns (payload, timed_out)."""
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", WORKER, str(path)],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            {
                "parse_result_code": "PARSE_FAILED",
                "failure_bucket": "TIMEOUT",
                "parse_time_ms": round(timeout * 1000.0, 3),
            },
            True,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    stdout = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not stdout:
        # Killed by a signal (OOM killer, segfault): a genuine worker failure.
        return (
            {
                "parse_result_code": "PARSE_FAILED",
                "failure_bucket": f"WORKER_EXIT_{proc.returncode}",
                "parse_time_ms": round(elapsed_ms, 3),
            },
            False,
        )
    try:
        return (json.loads(stdout.splitlines()[-1]), False)
    except (ValueError, IndexError):
        return (
            {"parse_result_code": "PARSE_FAILED", "failure_bucket": "WORKER_BAD_OUTPUT"},
            False,
        )


def measure_baseline_rss(timeout: float) -> float | None:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", WORKER, "--baseline"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return json.loads(proc.stdout.decode())["baseline_rss_mb"]
    except Exception:
        return None


def build_record(
    anonymous_id: str,
    path: Path,
    payload: dict,
    timed_out: bool,
    baseline_rss_mb: float | None = None,
) -> CorpusRecord:
    extension = path.suffix.lower().lstrip(".")
    container = detect_container(path)
    code = payload.get("parse_result_code")

    peak_rss = payload.get("peak_rss_mb")
    marginal = None
    if peak_rss is not None and baseline_rss_mb is not None:
        marginal = round(max(0.0, peak_rss - baseline_rss_mb), 3)

    # The selected parser is known even when parsing failed.
    parser_name = payload.get("parser_name")
    if parser_name is None:
        try:
            from document_processing.parsers import get_parser

            parser_name = get_parser(path).name
        except Exception:
            parser_name = None

    return CorpusRecord(
        anonymous_id=anonymous_id,
        format=extension,
        file_size_bytes=path.stat().st_size,
        parser_name=parser_name,
        parser_version=payload.get("parser_version"),
        parse_status=parse_status_for(code),
        parse_result_code=code,
        parse_time_ms=payload.get("parse_time_ms"),
        peak_memory_mb=payload.get("peak_memory_mb"),
        peak_rss_mb=peak_rss,
        rss_over_baseline_mb=marginal,
        paragraph_count=payload.get("paragraph_count"),
        table_count=payload.get("table_count"),
        table_cell_count=payload.get("table_cell_count"),
        text_length=payload.get("text_length"),
        paragraph_anchor_available=payload.get("paragraph_anchor_available"),
        section_title_count=payload.get("section_title_count"),
        normalized_text_hash=payload.get("normalized_text_hash"),
        canonical_hash=payload.get("canonical_hash"),
        warning_count=payload.get("warning_count", 0),
        warning_codes=payload.get("warning_codes", []),
        table_check=payload.get("table_check"),
        table_check_reasons=payload.get("table_check_reasons", []),
        container=container,
        extension_container_match=extension_matches_container(extension, container),
        failure_bucket=payload.get("failure_bucket"),
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# Determinism (section 13)
# ---------------------------------------------------------------------------

def pick_determinism_sample(
    records: list[CorpusRecord], id_to_path: dict[str, Path], per_format: int
) -> list[tuple[str, Path]]:
    """Pick a spread per format: largest, most tables, then plain documents."""
    chosen: list[tuple[str, Path]] = []
    for fmt in sorted({r.format for r in records}):
        ok = [
            r for r in records
            if r.format == fmt and r.parse_result_code == "TEXT_EXTRACTED"
        ]
        if not ok:
            continue
        picks: list[CorpusRecord] = []

        def add(rec: CorpusRecord | None) -> None:
            if rec is not None and rec.anonymous_id not in {p.anonymous_id for p in picks}:
                picks.append(rec)

        add(max(ok, key=lambda r: r.file_size_bytes))
        add(max(ok, key=lambda r: (r.table_count or 0)))
        add(max(ok, key=lambda r: (r.paragraph_count or 0)))
        for rec in ok:
            if len(picks) >= per_format:
                break
            add(rec)
        for rec in picks[:per_format]:
            chosen.append((rec.anonymous_id, id_to_path[rec.anonymous_id]))
    return chosen


def check_determinism(
    sample: list[tuple[str, Path]], runs: int, timeout: float
) -> list[dict]:
    results = []
    for anonymous_id, path in sample:
        payloads = [run_worker(path, timeout)[0] for _ in range(runs)]
        fields = ("canonical_hash", "normalized_text_hash", "paragraph_count", "table_count")
        stable = {
            field: len({json.dumps(p.get(field), sort_keys=True) for p in payloads}) == 1
            for field in fields
        }
        results.append(
            {
                "anonymous_id": anonymous_id,
                "runs": runs,
                "stable": all(stable.values()),
                "per_field": stable,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

AGGREGATE_COLUMNS = [
    "anonymous_id", "format", "file_size_bytes", "parser_name", "parser_version",
    "parse_status", "parse_result_code", "parse_time_ms", "peak_memory_mb",
    "peak_rss_mb", "rss_over_baseline_mb", "paragraph_count", "table_count", "table_cell_count",
    "text_length", "paragraph_anchor_available", "section_title_count",
    "normalized_text_hash", "warning_count", "warning_codes", "table_check",
    "container", "extension_container_match", "failure_bucket", "timed_out",
]


def write_aggregate_csv(path: Path, records: list[CorpusRecord]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AGGREGATE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            row = asdict(record)
            row["warning_codes"] = ";".join(record.warning_codes)
            writer.writerow(row)


def write_performance_csv(path: Path, rows: list[dict]) -> None:
    columns = [
        "bucket", "format", "documents",
        "parse_time_mean_ms", "parse_time_p50_ms", "parse_time_p95_ms", "parse_time_max_ms",
        "peak_rss_mean_mb", "peak_rss_p95_mb", "peak_rss_max_mb",
        "rss_over_baseline_mean_mb", "rss_over_baseline_p95_mb", "rss_over_baseline_max_mb",
        "ratio_sample_size", "rss_per_mb_mean", "rss_per_mb_p95", "rss_per_mb_max",
    ]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "bucket": row["bucket"],
                    "format": row["format"],
                    "documents": row["documents"],
                    "parse_time_mean_ms": row["parse_time_ms"]["mean"],
                    "parse_time_p50_ms": row["parse_time_ms"]["p50"],
                    "parse_time_p95_ms": row["parse_time_ms"]["p95"],
                    "parse_time_max_ms": row["parse_time_ms"]["max"],
                    "peak_rss_mean_mb": row["peak_rss_mb"]["mean"],
                    "peak_rss_p95_mb": row["peak_rss_mb"]["p95"],
                    "peak_rss_max_mb": row["peak_rss_mb"]["max"],
                    "rss_over_baseline_mean_mb": row["rss_over_baseline_mb"]["mean"],
                    "rss_over_baseline_p95_mb": row["rss_over_baseline_mb"]["p95"],
                    "rss_over_baseline_max_mb": row["rss_over_baseline_mb"]["max"],
                    "ratio_sample_size": row["ratio_sample_size"],
                    "rss_per_mb_mean": row["rss_per_mb_ratio"]["mean"],
                    "rss_per_mb_p95": row["rss_per_mb_ratio"]["p95"],
                    "rss_per_mb_max": row["rss_per_mb_ratio"]["max"],
                }
            )


def write_table_review_csv(path: Path, records: list[CorpusRecord]) -> None:
    """Worklist for the human spot-check required by section 10.

    Ordered worst-first so the reviewer starts where structure is most at risk.
    """
    order = {"FAIL": 0, "PASS_WITH_WARNING": 1, "PASS": 2}
    rows = [r for r in records if (r.table_count or 0) > 0]
    rows.sort(key=lambda r: (order.get(r.table_check or "", 9), -(r.table_count or 0)))
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["anonymous_id", "format", "table_count", "table_cell_count",
             "table_check", "table_check_reasons", "reviewed_by", "human_verdict", "notes"]
        )
        for r in rows:
            writer.writerow(
                [r.anonymous_id, r.format, r.table_count, r.table_cell_count,
                 r.table_check, ";".join(r.table_check_reasons), "", "", ""]
            )


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", type=Path, required=True, help="corpus directory (read-only)")
    ap.add_argument("--output", type=Path, required=True, help="directory for the artifacts")
    ap.add_argument("--timeout", type=float, default=300.0, help="per-document timeout, seconds")
    ap.add_argument("--limit", type=int, default=None, help="stop after N documents")
    ap.add_argument("--determinism-samples", type=int, default=5, help="documents per format")
    ap.add_argument("--determinism-runs", type=int, default=3)
    ap.add_argument("--no-determinism", action="store_true")
    ap.add_argument(
        "--mapping-file",
        type=Path,
        default=None,
        help="OPTIONAL: write anonymous_id -> real path here. Contains real paths; "
             "keep it internal and never commit it.",
    )
    ap.add_argument("--label", default=None, help="corpus description recorded in summary.json")
    args = ap.parse_args(argv)

    if not args.input.is_dir():
        print(f"error: --input {args.input} is not a directory", file=sys.stderr)
        return 2

    paths = sorted(
        p for p in args.input.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
    )
    if not paths:
        print(f"No .hwp/.hwpx files found under {args.input}", file=sys.stderr)
        return 2
    if args.limit:
        paths = paths[: args.limit]

    args.output.mkdir(parents=True, exist_ok=True)

    print(f"Corpus     : {len(paths)} documents")
    print(f"Timeout    : {args.timeout:.0f}s per document")
    print("Isolation  : one subprocess per document")
    print()

    baseline_rss = measure_baseline_rss(args.timeout)
    if baseline_rss:
        print(f"Baseline RSS (interpreter + imports): {baseline_rss:.1f} MB")
        print()

    pairs = assign_anonymous_ids(paths)
    id_to_path = {aid: p for aid, p in pairs}

    records: list[CorpusRecord] = []
    started = time.perf_counter()
    for n, (anonymous_id, path) in enumerate(pairs, start=1):
        payload, timed_out = run_worker(path, args.timeout)
        record = build_record(anonymous_id, path, payload, timed_out, baseline_rss)
        records.append(record)
        print(
            f"[{n:>4}/{len(pairs)}] {anonymous_id:<10} "
            f"{record.file_size_bytes/1048576:>7.2f} MB  "
            f"{record.parse_result_code or 'NONE':<19}"
            f"{(record.parse_time_ms or 0):>9.0f} ms",
            flush=True,
        )
    wall = time.perf_counter() - started

    determinism = []
    if not args.no_determinism:
        sample = pick_determinism_sample(records, id_to_path, args.determinism_samples)
        print(f"\nDeterminism: {len(sample)} documents x {args.determinism_runs} runs")
        determinism = check_determinism(sample, args.determinism_runs, args.timeout)

    formats = sorted({r.format for r in records})
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "corpus_label": args.label,
        "environment": {
            "os": f"{platform.system()} {platform.release()}",
            "python": sys.version.split()[0],
            "document_processing_version": __version__,
            "baseline_rss_mb": baseline_rss,
            "per_document_timeout_s": args.timeout,
        },
        "corpus": {
            "document_count": len(records),
            "by_format": {f: sum(1 for r in records if r.format == f) for f in formats},
            "total_bytes": sum(r.file_size_bytes for r in records),
            "size_bytes": {
                "min": min(r.file_size_bytes for r in records),
                "max": max(r.file_size_bytes for r in records),
            },
            "wall_clock_s": round(wall, 1),
        },
        "result_matrix": result_matrix(records),
        "overall": format_summary(records),
        "by_format": {f: format_summary(records, f) for f in formats},
        "performance_by_bucket": performance_by_bucket(records),
        "peak_rss_by_format": peak_rss_by_format(records),
        "memory_ratio_by_format": memory_ratio_by_format(records),
        "failure_buckets": failure_buckets(records),
        "container_mismatches": container_mismatches(records),
        "determinism": determinism,
    }

    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_aggregate_csv(args.output / "aggregate.csv", records)
    write_performance_csv(args.output / "performance.csv", summary["performance_by_bucket"])
    write_table_review_csv(args.output / "table_review.csv", records)

    if args.mapping_file:
        args.mapping_file.parent.mkdir(parents=True, exist_ok=True)
        args.mapping_file.write_text(
            json.dumps({aid: str(p) for aid, p in pairs}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n!! {args.mapping_file} contains real document paths. Keep it internal.")

    print_summary(summary)
    print(f"\nWrote {args.output}/summary.json, aggregate.csv, performance.csv, table_review.csv")
    return 0


def print_summary(summary: dict) -> None:
    print("\n" + "=" * 74)
    print("RESULT CODE DISTRIBUTION")
    print("-" * 74)
    formats = sorted(summary["corpus"]["by_format"])
    header = f"{'Result':<22}" + "".join(f"{f.upper():>9}" for f in formats) + f"{'Total':>9}{'Rate':>9}"
    print(header)
    total_docs = summary["corpus"]["document_count"]
    for code, row in summary["result_matrix"].items():
        if row["total"] == 0:
            continue
        line = f"{code:<22}" + "".join(f"{row.get(f, 0):>9}" for f in formats)
        line += f"{row['total']:>9}{row['total']/total_docs*100:>8.1f}%"
        print(line)
    print("-" * 74)
    for fmt in formats:
        stats = summary["by_format"][fmt]
        rate = stats.get("text_extracted_rate")
        anchor = stats.get("paragraph_anchor_rate")
        title = stats.get("section_title_rate")
        print(
            f"{fmt.upper():<6} n={stats['count']:<5} "
            f"TEXT_EXTRACTED={rate*100 if rate is not None else 0:>5.1f}%  "
            f"anchor={anchor*100 if anchor is not None else 0:>5.1f}%  "
            f"section_title={title*100 if title is not None else 0:>5.1f}%"
        )
    det = summary["determinism"]
    if det:
        stable = sum(1 for d in det if d["stable"])
        print(f"\nDeterminism : {stable}/{len(det)} sampled documents stable")
    if summary["container_mismatches"]:
        print(f"Extension/container mismatches: {summary['container_mismatches']}")
    print("=" * 74)


if __name__ == "__main__":
    raise SystemExit(main())
