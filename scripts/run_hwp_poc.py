#!/usr/bin/env python3
"""CLI for the HWP/HWPX parsing PoC.

    python scripts/run_hwp_poc.py tests/fixtures/hwp/simple_text.hwp
    python scripts/run_hwp_poc.py tests/fixtures/ --json artifacts/hwp-poc-results.json

A directory is walked recursively and every .hwp/.hwpx file is evaluated.
Failures are reported per file; the process exits non-zero only if *every*
file failed, so a batch run stays usable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_processing.poc import (  # noqa: E402
    FileResult,
    build_payload,
    evaluate_file,
    iter_target_files,
    summarize,
)
from document_processing.parsers import parse_document  # noqa: E402
from document_processing.normalize import build_normalized_text  # noqa: E402


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.0f}"
    return str(value)


def print_report(result: FileResult) -> None:
    print(f"File        : {result.filename}")
    print(f"Parser      : {result.parser or 'NONE'}")
    print(f"Status      : {result.status}", end="")
    if result.error_detail:
        print(f" ({result.error_detail})")
    else:
        print()
    if result.status != "SUCCESS":
        if result.error_message:
            print(f"Reason      : {result.error_message}")
        print()
        return

    print(f"Paragraphs  : {_fmt(result.paragraph_count)}")
    print(f"Tables      : {_fmt(result.table_count)} "
          f"({_fmt(result.table_cell_count)} cells)")
    print(f"Characters  : {_fmt(result.normalized_text_length)} (normalized)")
    print(f"Parse time  : {_fmt(result.parse_time_ms)} ms")
    print(f"Peak memory : {_fmt(result.peak_memory_bytes)} bytes")
    print()
    print("Anchors")
    print(f"  Section title   : {result.section_title_anchor}")
    print(f"  Page number     : {result.page_anchor}")
    print(f"  Paragraph index : {result.paragraph_anchor}")
    print()
    if result.deterministic is not None:
        verdict = "STABLE" if result.deterministic else "UNSTABLE"
        print(f"Determinism : {verdict} over {result.determinism_runs} runs")
        print(f"Content hash: {result.content_hash}")
        print()
    if result.warnings:
        print("Warnings:")
        for code in result.warnings:
            print(f"- {code}")
    else:
        print("Warnings: none")
    print()


def print_batch_summary(results: list[FileResult]) -> None:
    print("=" * 78)
    print(f"{'File':<44}{'Status':<20}{'Par':>5}{'Tbl':>5}")
    print("-" * 78)
    for r in results:
        name = r.filename if len(r.filename) <= 43 else r.filename[:40] + "..."
        print(
            f"{name:<44}{r.status:<20}"
            f"{_fmt(r.paragraph_count):>5}{_fmt(r.table_count):>5}"
        )
    print("-" * 78)
    summary = summarize(results)
    print(f"Files                 : {summary['file_count']}")
    print(f"Succeeded             : {summary['success_count']}")
    for status, count in summary["by_status"].items():
        if status != "SUCCESS":
            print(f"  {status:<20}: {count}")
    print(
        f"Deterministic         : {summary['deterministic_count']}"
        f"/{summary['determinism_checked_count']}"
    )
    print(
        f"Paragraph anchor      : {summary['paragraph_anchor_available_count']}"
        f"/{summary['success_count']}"
    )
    print(
        f"Page anchor           : {summary['page_anchor_available_count']}"
        f"/{summary['success_count']}"
    )
    print(
        f"Section title anchor  : {summary['section_title_available_count']}"
        f"/{summary['success_count']}"
    )
    print(f"Total parse time      : {_fmt(summary['total_parse_time_ms'])} ms")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", type=Path, help="a .hwp/.hwpx file or a directory")
    parser.add_argument(
        "--json", type=Path, default=None, help="write full results to this JSON file"
    )
    parser.add_argument(
        "--text", action="store_true", help="print the normalized text (single file only)"
    )
    parser.add_argument(
        "--label",
        default=None,
        help="describe the corpus in the JSON artifact (e.g. 'public sample corpus')",
    )
    parser.add_argument(
        "--no-determinism",
        action="store_true",
        help="skip the repeat-parse determinism check (faster)",
    )
    args = parser.parse_args(argv)

    if not args.target.exists():
        print(f"error: {args.target} does not exist", file=sys.stderr)
        return 2

    files = list(iter_target_files(args.target))
    if not files:
        print(f"No .hwp/.hwpx files found under {args.target}")
        print("See tests/fixtures/README.md for how to add test documents.")
        return 0

    check = not args.no_determinism
    base = args.target if args.target.is_dir() else args.target.parent
    results = [
        evaluate_file(p, check_determinism=check, relative_to=base) for p in files
    ]

    if len(results) == 1:
        print_report(results[0])
        if args.text and results[0].status == "SUCCESS":
            print("-" * 78)
            print(build_normalized_text(parse_document(files[0])))
    else:
        print_batch_summary(results)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = build_payload(results, args.target, args.label)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Wrote {args.json}")

    return 0 if any(r.status == "SUCCESS" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
