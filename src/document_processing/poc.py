"""PoC evaluation harness: benchmark one file or a whole directory.

Kept in the package (rather than in the script) so tests can exercise it.
Nothing here belongs in production ingestion -- it exists to answer the PoC
questions and to produce ``artifacts/hwp-poc-results.json``.
"""

from __future__ import annotations

import platform
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .models import ParsedDocument
from .normalize import build_normalized_text, content_hash, normalized_text_hash
from .parsers import DocumentParseError, get_parser, parse_document
from .parsers.exceptions import UnsupportedFormatError

SUPPORTED_SUFFIXES = (".hwp", ".hwpx")

#: How many times a file is re-parsed when determinism is being checked.
DETERMINISM_RUNS = 3


@dataclass
class FileResult:
    """One file's benchmark row."""

    filename: str
    path: str
    file_type: str
    file_size_bytes: int

    status: str = "SUCCESS"          # SUCCESS or the error code
    error_detail: str | None = None
    error_message: str | None = None

    parser: str | None = None
    parser_version: str | None = None

    parse_time_ms: float | None = None
    peak_memory_bytes: int | None = None

    paragraph_count: int | None = None
    table_count: int | None = None
    table_cell_count: int | None = None
    paragraph_text_length: int | None = None
    normalized_text_length: int | None = None

    page_anchor: str = "NOT_AVAILABLE"       # AVAILABLE / NOT_AVAILABLE
    paragraph_anchor: str = "NOT_AVAILABLE"
    section_title_anchor: str = "NOT_AVAILABLE"

    content_hash: str | None = None
    normalized_text_hash: str | None = None

    deterministic: bool | None = None
    determinism_runs: int | None = None

    warnings: list[str] = field(default_factory=list)


def _measure(path: Path) -> tuple[ParsedDocument, float, int]:
    tracemalloc.start()
    started = time.perf_counter()
    try:
        doc = parse_document(path)
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return doc, elapsed_ms, peak


def evaluate_file(
    path: Path, *, check_determinism: bool = True, relative_to: Path | None = None
) -> FileResult:
    """Parse one file and collect its PoC row.

    Never raises for a document-level problem: an unreadable file becomes a row
    with a status code.  This is the behaviour an ingestion worker needs -- one
    bad file must not stop a batch.
    """
    path = Path(path)
    # Record a relative path so the artifact does not leak machine-specific
    # directory layout and stays comparable across runs.
    display_path = str(path)
    if relative_to is not None:
        try:
            display_path = str(path.relative_to(relative_to))
        except ValueError:
            pass
    result = FileResult(
        filename=path.name,
        path=display_path,
        file_type=path.suffix.lower().lstrip("."),
        file_size_bytes=path.stat().st_size if path.exists() else 0,
    )
    try:
        result.parser = get_parser(path).name
    except UnsupportedFormatError:
        result.status = "UNSUPPORTED_FORMAT"
        result.error_message = f"no parser for '{path.suffix}'"
        return result

    try:
        doc, elapsed_ms, peak = _measure(path)
    except DocumentParseError as exc:
        result.status = exc.error_code
        result.error_detail = exc.detail
        result.error_message = exc.message
        return result
    except Exception as exc:  # pragma: no cover - parsers already wrap these
        result.status = "PARSE_FAILED"
        result.error_detail = type(exc).__name__
        result.error_message = str(exc)
        return result

    normalized = build_normalized_text(doc)
    result.parser_version = doc.parser_version
    result.parse_time_ms = round(elapsed_ms, 3)
    result.peak_memory_bytes = peak
    result.paragraph_count = doc.paragraph_count
    result.table_count = doc.table_count
    result.table_cell_count = sum(len(t.cells) for t in doc.tables)
    result.paragraph_text_length = doc.text_length
    result.normalized_text_length = len(normalized)
    result.page_anchor = "AVAILABLE" if doc.has_page_anchors() else "NOT_AVAILABLE"
    result.paragraph_anchor = "AVAILABLE" if doc.paragraphs else "NOT_AVAILABLE"
    result.section_title_anchor = (
        "AVAILABLE" if doc.has_section_titles() else "NOT_AVAILABLE"
    )
    result.content_hash = content_hash(doc)
    result.normalized_text_hash = normalized_text_hash(doc)
    result.warnings = sorted(doc.warnings)

    if check_determinism:
        hashes = {result.content_hash}
        for _ in range(DETERMINISM_RUNS - 1):
            try:
                hashes.add(content_hash(parse_document(path)))
            except DocumentParseError:
                hashes.add("PARSE_FAILED")
        result.determinism_runs = DETERMINISM_RUNS
        result.deterministic = len(hashes) == 1

    return result


def iter_target_files(target: Path) -> Iterable[Path]:
    target = Path(target)
    if target.is_file():
        yield target
        return
    for path in sorted(target.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def environment() -> dict[str, Any]:
    try:
        import olefile

        olefile_version = getattr(olefile, "__version__", "unknown")
    except ImportError:  # pragma: no cover
        olefile_version = "not installed"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "document_processing_version": __version__,
        "olefile_version": olefile_version,
    }


def summarize(results: list[FileResult]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    successes = [r for r in results if r.status == "SUCCESS"]
    checked = [r for r in successes if r.deterministic is not None]
    times = [r.parse_time_ms for r in successes if r.parse_time_ms is not None]
    return {
        "file_count": len(results),
        "by_status": dict(sorted(by_status.items())),
        "success_count": len(successes),
        "deterministic_count": sum(1 for r in checked if r.deterministic),
        "determinism_checked_count": len(checked),
        "page_anchor_available_count": sum(
            1 for r in successes if r.page_anchor == "AVAILABLE"
        ),
        "paragraph_anchor_available_count": sum(
            1 for r in successes if r.paragraph_anchor == "AVAILABLE"
        ),
        "section_title_available_count": sum(
            1 for r in successes if r.section_title_anchor == "AVAILABLE"
        ),
        "total_parse_time_ms": round(sum(times), 3),
        "max_parse_time_ms": round(max(times), 3) if times else None,
    }


def build_payload(
    results: list[FileResult], target: Path, label: str | None = None
) -> dict[str, Any]:
    """Assemble the JSON artifact written to ``artifacts/hwp-poc-results.json``."""
    return {
        "environment": environment(),
        "target": str(target),
        "corpus_label": label,
        "summary": summarize(results),
        "results": [asdict(r) for r in results],
    }


def run(
    target: Path, *, check_determinism: bool = True, label: str | None = None
) -> dict[str, Any]:
    base = target if target.is_dir() else target.parent
    results = [
        evaluate_file(p, check_determinism=check_determinism, relative_to=base)
        for p in iter_target_files(target)
    ]
    return build_payload(results, target, label)
