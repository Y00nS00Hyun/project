"""Integration tests: real HWP/HWPX documents.

Every test here skips when its fixture is absent -- a missing document must
never look like a pass.  See tests/fixtures/README.md for what to author, and
set ``HWP_POC_FIXTURE_DIR`` to run against a corpus kept outside the repo.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from conftest import EXPECTED_ROOT, require_fixture
from document_processing.normalize import build_normalized_text, content_hash
from document_processing.parsers import parse_document
from document_processing.parsers.exceptions import DocumentParseError

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Metadata-driven cases
# ---------------------------------------------------------------------------

def load_specs() -> list[dict]:
    specs = []
    for path in sorted(EXPECTED_ROOT.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["_spec_name"] = path.stem
        specs.append(spec)
    return specs


SPECS = load_specs()


def table_texts(doc) -> str:
    return "\n".join("\n".join(row) for table in doc.tables for row in table.rows)


@pytest.mark.parametrize("spec", SPECS, ids=[s["_spec_name"] for s in SPECS])
def test_fixture_matches_expected_metadata(spec):
    path = require_fixture(spec["filename"])
    expected_status = spec.get("expect_status", "SUCCESS")

    started = time.perf_counter()
    try:
        doc = parse_document(path)
        status = "SUCCESS"
        error = None
    except DocumentParseError as exc:
        doc = None
        status = exc.error_code
        error = exc
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert status == expected_status, (
        f"{spec['filename']}: expected {expected_status}, got {status}"
        + (f" ({error})" if error else "")
    )

    if status != "SUCCESS":
        if "expect_error_detail" in spec:
            assert error.detail == spec["expect_error_detail"]
        return

    rules = spec.get("expected", {})
    normalized = build_normalized_text(doc)
    tables = table_texts(doc)

    if "min_paragraphs" in rules:
        assert doc.paragraph_count >= rules["min_paragraphs"]
    if "min_tables" in rules:
        assert doc.table_count >= rules["min_tables"]
    if "min_sections" in rules:
        sections = {p.section_index for p in doc.paragraphs if p.section_index is not None}
        assert len(sections) >= rules["min_sections"]

    for needle in rules.get("contains_text", []):
        assert needle in normalized, f"'{needle}' missing from normalized text"
    for needle in rules.get("table_contains", []):
        assert needle in tables, f"'{needle}' missing from table content"

    ordered = rules.get("ordered_text", [])
    positions = [normalized.find(n) for n in ordered]
    assert all(p >= 0 for p in positions), f"missing ordered text in {spec['filename']}"
    assert positions == sorted(positions), "text did not appear in document order"

    for code in rules.get("expect_warnings", []):
        assert code in doc.warnings, f"expected warning {code}, got {doc.warnings}"

    grid = rules.get("table_grid")
    if grid:
        table = doc.tables[grid["table_index"]]
        assert table.row_count >= grid.get("min_rows", 0)
        assert table.column_count >= grid.get("min_columns", 0)
        for key, expected_row in grid.items():
            if key.startswith("row_"):
                index = int(key.split("_", 1)[1])
                assert table.rows[index][: len(expected_row)] == expected_row

    if "max_parse_time_ms" in rules:
        assert elapsed_ms <= rules["max_parse_time_ms"]

    # Paragraph anchors are the one guarantee this PoC makes.
    assert [p.index for p in doc.paragraphs] == list(range(doc.paragraph_count))


# ---------------------------------------------------------------------------
# Named cases from the PoC brief
# ---------------------------------------------------------------------------

def test_hwp_simple_text():
    doc = parse_document(require_fixture("hwp/simple_text.hwp"))
    assert doc.file_type == "hwp"
    assert doc.paragraph_count > 0
    assert any(p.text.strip() for p in doc.paragraphs)


def test_hwpx_simple_text():
    doc = parse_document(require_fixture("hwpx/simple_text.hwpx"))
    assert doc.file_type == "hwpx"
    assert doc.paragraph_count > 0
    assert any(p.text.strip() for p in doc.paragraphs)


def test_hwp_table():
    doc = parse_document(require_fixture("hwp/table.hwp"))
    assert doc.table_count >= 1
    table = doc.tables[0]
    assert table.row_count >= 2 and table.column_count >= 2
    assert any(cell.strip() for row in table.rows for cell in row)


def test_hwpx_table():
    doc = parse_document(require_fixture("hwpx/table.hwpx"))
    assert doc.table_count >= 1
    table = doc.tables[0]
    assert table.row_count >= 2 and table.column_count >= 2
    assert any(cell.strip() for row in table.rows for cell in row)


@pytest.mark.parametrize("relative", ["hwp/korean_numbers.hwp", "hwpx/korean_numbers.hwpx"])
def test_korean_characters_preserved(relative):
    doc = parse_document(require_fixture(relative))
    text = build_normalized_text(doc)
    assert "제1조(목적)" in text
    assert "㈜한국" in text
    # No replacement characters and no mojibake markers.
    assert "�" not in text


@pytest.mark.parametrize("relative", ["hwp/korean_numbers.hwp", "hwpx/korean_numbers.hwpx"])
def test_numbers_preserved(relative):
    doc = parse_document(require_fixture(relative))
    text = build_normalized_text(doc)
    assert "300,000,000원" in text
    assert "2026-01-15" in text
    assert "50.5%" in text


@pytest.mark.parametrize("relative", ["hwp/corrupt.hwp", "hwpx/corrupt.hwpx"])
def test_corrupt_file(relative):
    path = require_fixture(relative)
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document(path)
    assert excinfo.value.error_code in {"CORRUPT", "PARSE_FAILED"}


def test_encrypted_file():
    path = require_fixture("hwp/encrypted.hwp")
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document(path)
    assert excinfo.value.error_code == "ENCRYPTED"


def test_scan_like_file_is_flagged_for_ocr():
    path = require_fixture("hwp/scan_like.hwp")
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document(path)
    assert excinfo.value.error_code == "OCR_REQUIRED"


@pytest.mark.parametrize(
    "relative",
    [
        "hwp/simple_text.hwp",
        "hwp/table.hwp",
        "hwpx/simple_text.hwpx",
        "hwpx/table.hwpx",
    ],
)
def test_idempotent_parse(relative):
    """Parsing the same bytes three times must give byte-identical results."""
    path = require_fixture(relative)
    docs = [parse_document(path) for _ in range(3)]

    assert len({content_hash(d) for d in docs}) == 1
    assert len({d.paragraph_count for d in docs}) == 1
    assert len({d.table_count for d in docs}) == 1
    assert len({build_normalized_text(d) for d in docs}) == 1
    assert len({tuple(tuple(r) for t in d.tables for r in t.rows) for d in docs}) == 1


def test_worker_survives_a_mixed_batch(fixtures_dir: Path):
    """A bad file must fail alone, never take the batch down.

    Runs over whatever fixtures exist; skips only if there are none at all.
    """
    files = sorted(
        p for p in fixtures_dir.rglob("*") if p.suffix.lower() in {".hwp", ".hwpx"}
    )
    if not files:
        pytest.skip("no fixtures present; see tests/fixtures/README.md")

    statuses = []
    for path in files:
        try:
            parse_document(path)
            statuses.append("SUCCESS")
        except DocumentParseError as exc:
            statuses.append(exc.error_code)
        # Deliberately no bare `except`: anything else escaping is a bug.
    assert len(statuses) == len(files)
