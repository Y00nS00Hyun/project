"""Parse exactly one document in this process and report measurements as JSON.

Run as ``python -m document_processing.corpus_worker <path>``.

Running one document per process is deliberate:

* ``ru_maxrss`` then measures *this document's* peak resident memory rather
  than a high-water mark polluted by every file parsed before it, which is what
  worker memory sizing actually needs;
* a runaway document can be killed on a timeout by the parent;
* it mirrors the production arrangement in which a parser failure must not take
  the service down (functional spec v2.3 section 21.4).

The output carries counts, hashes and codes only -- never document text.
"""

from __future__ import annotations

import json
import resource
import sys
import time
import tracemalloc
from pathlib import Path

from .corpus import check_paragraph_anchors, check_tables, sanitize_reason
from .normalize import build_normalized_text, content_hash, normalized_text_hash
from .parsers import parse_document
from .parsers.exceptions import DocumentParseError


def _peak_rss_mb() -> float:
    # Linux reports ru_maxrss in kilobytes.
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 3)


def measure(path: Path) -> dict:
    out: dict = {"ok": True}

    tracemalloc.start()
    started = time.perf_counter()
    try:
        doc = parse_document(path)
    except DocumentParseError as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "ok": True,
            "parse_result_code": exc.error_code,
            "failure_bucket": sanitize_reason(exc.message, exc.detail),
            "parse_time_ms": round(elapsed, 3),
            "peak_memory_mb": round(peak / (1 << 20), 3),
            "peak_rss_mb": _peak_rss_mb(),
        }
    elapsed = (time.perf_counter() - started) * 1000.0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    normalized = build_normalized_text(doc)
    table_verdict, table_reasons = check_tables(doc)

    out.update(
        {
            "parse_result_code": "TEXT_EXTRACTED",
            "parser_name": doc.parser_name,
            "parser_version": doc.parser_version,
            "parse_time_ms": round(elapsed, 3),
            "peak_memory_mb": round(peak / (1 << 20), 3),
            "peak_rss_mb": _peak_rss_mb(),
            "paragraph_count": doc.paragraph_count,
            "table_count": doc.table_count,
            "table_cell_count": sum(len(t.cells) for t in doc.tables),
            "text_length": len(normalized),
            "paragraph_anchor_available": check_paragraph_anchors(doc),
            "section_title_count": sum(1 for p in doc.paragraphs if p.section_title),
            "normalized_text_hash": normalized_text_hash(doc),
            "canonical_hash": content_hash(doc),
            "warning_codes": sorted(doc.warnings),
            "warning_count": len(doc.warnings),
            "table_check": table_verdict,
            "table_check_reasons": table_reasons,
        }
    )
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: corpus_worker <path>|--baseline"}))
        return 2
    if argv[1] == "--baseline":
        # Interpreter + package import cost, so per-document RSS can be
        # reported net of it.
        print(json.dumps({"ok": True, "baseline_rss_mb": _peak_rss_mb()}))
        return 0
    try:
        payload = measure(Path(argv[1]))
    except Exception as exc:  # noqa: BLE001 - report, never crash silently
        payload = {
            "ok": False,
            "parse_result_code": "PARSE_FAILED",
            "failure_bucket": sanitize_reason(str(exc), type(exc).__name__),
            "peak_rss_mb": _peak_rss_mb(),
        }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
