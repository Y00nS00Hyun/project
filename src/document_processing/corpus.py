"""Corpus-scale measurement for the internal HWP/HWPX re-measurement.

This module exists to answer one question with real numbers: *may we use this
parser for internal document ingestion?*  It is measurement code, not
production ingestion code.

Privacy contract -- every record this module emits is safe to share outside the
internal network:

* documents are identified only by a generated ``HWP-001`` / ``HWPX-001`` id;
* no filename, no path, no document text ever reaches a record;
* failure reasons are reduced to a bucket label with paths and quoted names
  stripped and digits collapsed.

The path -> anonymous-id mapping is written only when the operator explicitly
asks for it, to a file they name, and never into the shared artifacts.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# parse_status / parse_result_code  (functional spec v2.3 section 8.2)
# ---------------------------------------------------------------------------

#: A successful parse means "the parser produced usable body text".
RESULT_TEXT_EXTRACTED = "TEXT_EXTRACTED"

#: All result codes, in report order.  Everything after TEXT_EXTRACTED is a
#: parser error code, so the two taxonomies cannot drift apart silently.
RESULT_CODES: tuple[str, ...] = (RESULT_TEXT_EXTRACTED,) + tuple(
    code
    for code in ("EMPTY_DOCUMENT", "OCR_REQUIRED", "ENCRYPTED", "CORRUPT",
                 "UNSUPPORTED_FORMAT", "PARSE_FAILED")
)

#: Result codes the parser reached by *correctly determining* something about
#: the file.  Per spec v2.3 section 8.2 these are worker successes: the worker
#: did its job.  Only an unattributable failure is a worker failure.
_DETERMINED_RESULTS = frozenset(
    {
        RESULT_TEXT_EXTRACTED,
        "EMPTY_DOCUMENT",
        "OCR_REQUIRED",
        "ENCRYPTED",
        "CORRUPT",
        "UNSUPPORTED_FORMAT",
    }
)


def parse_status_for(result_code: str | None) -> str:
    """Map a result code to the worker execution status.

    ``PARSE_FAILED`` is the only code that means the worker itself failed;
    everything else is a determination the parser made on purpose.
    """
    if result_code is None:
        return "PENDING"
    return "SUCCESS" if result_code in _DETERMINED_RESULTS else "FAILED"


def downstream_status_for(result_code: str | None) -> str:
    """What embedding_status would be set to for this parse outcome."""
    if result_code == RESULT_TEXT_EXTRACTED:
        return "PENDING"
    if result_code is None:
        return "PENDING"
    return "SKIPPED"


# ---------------------------------------------------------------------------
# Container sniffing (Open Issue OI-6: extension vs real format)
# ---------------------------------------------------------------------------

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


def detect_container(path: Path) -> str:
    """Identify the real container from the file header.

    Returns ``"ole"`` (HWP 5.x), ``"zip"`` (HWPX), ``"empty"`` or ``"unknown"``.
    Never raises.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return "unknown"
    if not head:
        return "empty"
    if head.startswith(_OLE_MAGIC):
        return "ole"
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    return "unknown"


#: Which container each extension is supposed to have.
_EXPECTED_CONTAINER = {"hwp": "ole", "hwpx": "zip"}


def extension_matches_container(extension: str, container: str) -> bool | None:
    """``None`` when we have no expectation for this extension."""
    expected = _EXPECTED_CONTAINER.get(extension)
    if expected is None:
        return None
    return container == expected


# ---------------------------------------------------------------------------
# Failure reason bucketing (section 19) -- must not leak document identity
# ---------------------------------------------------------------------------

_PATH_RE = re.compile(r"(/[^\s'\"]+)+|([A-Za-z]:\\[^\s'\"]+)")
_QUOTED_RE = re.compile(r"['\"][^'\"]*['\"]")
_NUM_RE = re.compile(r"\d+")


def sanitize_reason(message: str | None, detail: str | None) -> str:
    """Reduce an error to a shareable bucket label.

    Absolute paths and quoted names are removed outright and digits are
    collapsed, so a bucket can never carry a filename, a path or a byte offset
    that identifies one document.
    """
    if detail:
        base = detail
    elif message:
        base = message
    else:
        return "UNKNOWN"
    base = _PATH_RE.sub(" ", base)
    base = _QUOTED_RE.sub(" ", base)
    base = _NUM_RE.sub("N", base)
    base = re.sub(r"\s+", " ", base).strip(" .:;,-")
    return (base[:80] or "UNKNOWN")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class CorpusRecord:
    """One document's measurement. Safe to share outside the network."""

    anonymous_id: str
    format: str
    file_size_bytes: int

    parser_name: str | None = None
    parser_version: str | None = None

    parse_status: str = "PENDING"
    parse_result_code: str | None = None

    parse_time_ms: float | None = None
    peak_memory_mb: float | None = None      # Python allocations (tracemalloc)
    peak_rss_mb: float | None = None         # whole-process peak RSS
    rss_over_baseline_mb: float | None = None  # peak RSS minus interpreter baseline

    paragraph_count: int | None = None
    table_count: int | None = None
    table_cell_count: int | None = None
    text_length: int | None = None

    paragraph_anchor_available: bool | None = None
    section_title_count: int | None = None

    normalized_text_hash: str | None = None
    canonical_hash: str | None = None

    warning_count: int = 0
    warning_codes: list[str] = field(default_factory=list)

    # Structural self-check for table documents (section 10).
    table_check: str | None = None          # PASS / PASS_WITH_WARNING / FAIL
    table_check_reasons: list[str] = field(default_factory=list)

    # OI-6
    container: str | None = None
    extension_container_match: bool | None = None

    # Failure analysis (section 19); bucket only, never a message.
    failure_bucket: str | None = None

    timed_out: bool = False


def assign_anonymous_ids(paths: Sequence[Path]) -> list[tuple[str, Path]]:
    """Assign stable ``HWP-001`` / ``HWPX-001`` ids.

    Ordering is by extension then by a hash of the path, so ids do not leak
    directory structure or alphabetical filenames, yet stay stable across
    re-runs of the same corpus.
    """
    buckets: dict[str, list[Path]] = {}
    for path in paths:
        ext = path.suffix.lower().lstrip(".")
        buckets.setdefault(ext, []).append(path)

    out: list[tuple[str, Path]] = []
    for ext in sorted(buckets):
        ordered = sorted(buckets[ext], key=lambda p: hashlib.sha256(str(p).encode()).hexdigest())
        prefix = ext.upper()
        for n, path in enumerate(ordered, start=1):
            out.append((f"{prefix}-{n:03d}", path))
    return out


# ---------------------------------------------------------------------------
# Table structural check (section 10)
# ---------------------------------------------------------------------------

#: Warnings that mean the recovered grid disagrees with what the file declares.
_TABLE_FAIL_WARNINGS = frozenset(
    {
        "TABLE_ROW_COUNT_MISMATCH",
        "TABLE_COLUMN_COUNT_MISMATCH",
        "TABLE_CELL_COUNT_MISMATCH",
        "MERGED_CELL_STRUCTURE_LOSS",
        "TABLE_CELL_ADDRESS_COLLISION",
    }
)

#: Warnings that mean the grid is intact but something is worth a human look.
_TABLE_WARN_WARNINGS = frozenset(
    {
        "MERGED_CELLS_PRESENT",
        "NESTED_TABLE_FLATTENED",
        "TABLE_CELL_ADDRESS_UNREADABLE",
    }
)


def check_tables(doc) -> tuple[str, list[str]]:
    """Objective structural verdict for a document's tables.

    This is a self-check against what the *file itself declares*, not a
    judgement about whether the content is right -- that still needs a human
    spot-check, which is why the runner emits a review worklist.
    """
    if not doc.tables:
        return ("PASS", [])

    reasons: list[str] = []
    verdict = "PASS"

    for code in sorted(set(doc.warnings)):
        if code in _TABLE_FAIL_WARNINGS:
            verdict = "FAIL"
            reasons.append(code)
        elif code in _TABLE_WARN_WARNINGS and verdict != "FAIL":
            verdict = "PASS_WITH_WARNING"
            reasons.append(code)

    for table in doc.tables:
        if table.declared_row_count and table.row_count != table.declared_row_count:
            verdict = "FAIL"
            reasons.append("ROW_COUNT_DISAGREES_WITH_FILE")
        if table.declared_column_count and table.column_count != table.declared_column_count:
            verdict = "FAIL"
            reasons.append("COLUMN_COUNT_DISAGREES_WITH_FILE")
        if table.cells and not table.rows:
            verdict = "FAIL"
            reasons.append("CELLS_PRESENT_BUT_GRID_EMPTY")
        # Ordering guarantee: body/table interleaving must be reconstructable.
        if table.paragraph_index is None and doc.paragraphs:
            verdict = "FAIL" if verdict == "FAIL" else "PASS_WITH_WARNING"
            reasons.append("TABLE_NOT_ANCHORED_TO_PARAGRAPH")

    return (verdict, sorted(set(reasons)))


def check_paragraph_anchors(doc) -> bool:
    """Paragraph indices must be a complete 0..n-1 run."""
    return [p.index for p in doc.paragraphs] == list(range(len(doc.paragraphs)))


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

#: Below this size, peak RSS is dominated by the fixed interpreter baseline
#: rather than by the document, so a "memory per MB" ratio is not meaningful.
#: Ratios are reported only for documents at or above this size.
RATIO_MIN_FILE_BYTES = 1 << 20  # 1 MB

#: Performance buckets in bytes; adjustable per corpus distribution.
SIZE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("< 1 MB", 0, 1 << 20),
    ("1-5 MB", 1 << 20, 5 << 20),
    ("5-20 MB", 5 << 20, 20 << 20),
    ("20-50 MB", 20 << 20, 50 << 20),
    ("> 50 MB", 50 << 20, 1 << 62),
)


def bucket_for(size_bytes: int) -> str:
    for label, low, high in SIZE_BUCKETS:
        if low <= size_bytes < high:
            return label
    return SIZE_BUCKETS[-1][0]


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile. Returns ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = max(1, min(len(ordered), int(round(pct / 100.0 * len(ordered) + 0.5))))
    return round(ordered[rank - 1], 3)


def _stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "mean": round(statistics.fmean(values), 3),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": round(max(values), 3),
    }


def result_matrix(records: Iterable[CorpusRecord]) -> dict[str, dict[str, int]]:
    """result_code -> {format -> count, 'total' -> count}."""
    records = list(records)
    formats = sorted({r.format for r in records})
    matrix: dict[str, dict[str, int]] = {}
    for code in RESULT_CODES:
        row = {fmt: 0 for fmt in formats}
        row["total"] = 0
        matrix[code] = row
    for r in records:
        code = r.parse_result_code or "PARSE_FAILED"
        row = matrix.setdefault(code, {fmt: 0 for fmt in formats} | {"total": 0})
        row[r.format] = row.get(r.format, 0) + 1
        row["total"] += 1
    return matrix


def format_summary(records: Sequence[CorpusRecord], fmt: str | None = None) -> dict[str, Any]:
    subset = [r for r in records if fmt is None or r.format == fmt]
    total = len(subset)
    extracted = [r for r in subset if r.parse_result_code == RESULT_TEXT_EXTRACTED]
    if total == 0:
        return {"count": 0}

    anchors = [r for r in extracted if r.paragraph_anchor_available]
    with_titles = [r for r in extracted if (r.section_title_count or 0) > 0]
    with_tables = [r for r in extracted if (r.table_count or 0) > 0]
    table_verdicts: dict[str, int] = {}
    for r in with_tables:
        table_verdicts[r.table_check or "UNKNOWN"] = table_verdicts.get(r.table_check or "UNKNOWN", 0) + 1

    return {
        "count": total,
        "text_extracted": len(extracted),
        "text_extracted_rate": round(len(extracted) / total, 4),
        "worker_failed": sum(1 for r in subset if r.parse_status == "FAILED"),
        "timed_out": sum(1 for r in subset if r.timed_out),
        "paragraph_anchor_available": len(anchors),
        "paragraph_anchor_rate": round(len(anchors) / len(extracted), 4) if extracted else None,
        "documents_with_section_title": len(with_titles),
        "section_title_rate": round(len(with_titles) / len(extracted), 4) if extracted else None,
        "documents_with_tables": len(with_tables),
        "table_check": table_verdicts,
        "parse_time_ms": _stats([r.parse_time_ms for r in extracted if r.parse_time_ms is not None]),
        "peak_rss_mb": _stats([r.peak_rss_mb for r in extracted if r.peak_rss_mb is not None]),
        "peak_python_mb": _stats([r.peak_memory_mb for r in extracted if r.peak_memory_mb is not None]),
    }


def performance_by_bucket(records: Sequence[CorpusRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, _low, _high in SIZE_BUCKETS:
        for fmt in sorted({r.format for r in records}):
            subset = [
                r
                for r in records
                if r.format == fmt
                and bucket_for(r.file_size_bytes) == label
                and r.parse_result_code == RESULT_TEXT_EXTRACTED
            ]
            if not subset:
                continue
            times = [r.parse_time_ms for r in subset if r.parse_time_ms is not None]
            rss = [r.peak_rss_mb for r in subset if r.peak_rss_mb is not None]
            marginal = [
                r.rss_over_baseline_mb
                for r in subset
                if r.rss_over_baseline_mb is not None
            ]
            ratios = _ratios(subset)
            rows.append(
                {
                    "bucket": label,
                    "format": fmt,
                    "documents": len(subset),
                    "parse_time_ms": _stats(times),
                    "peak_rss_mb": _stats(rss),
                    "rss_over_baseline_mb": _stats(marginal),
                    "rss_per_mb_ratio": _stats(ratios),
                    "ratio_sample_size": len(ratios),
                }
            )
    return rows


def _ratios(records: Sequence[CorpusRecord]) -> list[float]:
    """Marginal RSS per MB of on-disk file size.

    Uses RSS *over the interpreter baseline*, because the baseline is paid once
    per worker process and is not attributable to the document. Documents below
    ``RATIO_MIN_FILE_BYTES`` are excluded: for them the ratio measures fixed
    overhead, not the parser.
    """
    out = []
    for r in records:
        if (
            r.parse_result_code != RESULT_TEXT_EXTRACTED
            or r.rss_over_baseline_mb is None
            or r.file_size_bytes < RATIO_MIN_FILE_BYTES
        ):
            continue
        out.append(r.rss_over_baseline_mb / (r.file_size_bytes / (1 << 20)))
    return out


def memory_ratio_by_format(records: Sequence[CorpusRecord]) -> dict[str, Any]:
    """Marginal peak RSS per MB of on-disk file size, per format.

    Reported alongside the sample size, because with few large documents this
    ratio must not be turned into a production sizing formula on its own.
    """
    out: dict[str, Any] = {}
    for fmt in sorted({r.format for r in records}):
        subset = [r for r in records if r.format == fmt]
        ratios = _ratios(subset)
        stats = _stats(ratios)
        stats["note"] = (
            f"documents >= {RATIO_MIN_FILE_BYTES // (1 << 20)} MB only; "
            f"RSS measured over interpreter baseline"
        )
        out[fmt] = stats
    return out


def peak_rss_by_format(records: Sequence[CorpusRecord]) -> dict[str, Any]:
    """Absolute peak RSS per format -- the figure a worker memory limit is set from."""
    out: dict[str, Any] = {}
    for fmt in sorted({r.format for r in records}):
        values = [
            r.peak_rss_mb
            for r in records
            if r.format == fmt and r.peak_rss_mb is not None
        ]
        out[fmt] = _stats(values)
    return out


def failure_buckets(records: Iterable[CorpusRecord]) -> dict[str, dict[str, int]]:
    """result_code -> {sanitised reason bucket -> count}."""
    out: dict[str, dict[str, int]] = {}
    for r in records:
        code = r.parse_result_code
        if code in (None, RESULT_TEXT_EXTRACTED):
            continue
        bucket = r.failure_bucket or "UNKNOWN"
        out.setdefault(code, {})
        out[code][bucket] = out[code].get(bucket, 0) + 1
    return out


def container_mismatches(records: Iterable[CorpusRecord]) -> dict[str, int]:
    """OI-6: how often the extension disagrees with the real container."""
    out: dict[str, int] = {}
    for r in records:
        if r.extension_container_match is False:
            key = f"{r.format} -> {r.container}"
            out[key] = out.get(key, 0) + 1
    return out
