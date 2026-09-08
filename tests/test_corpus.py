"""Unit tests for the internal-corpus measurement layer.

The privacy tests here are the important ones: they are what lets a measurement
run on real internal documents produce artifacts that can leave the network.
"""

from __future__ import annotations

import dataclasses

import pytest

from document_processing.corpus import (
    RATIO_MIN_FILE_BYTES,
    RESULT_CODES,
    CorpusRecord,
    assign_anonymous_ids,
    bucket_for,
    check_paragraph_anchors,
    check_tables,
    container_mismatches,
    detect_container,
    downstream_status_for,
    extension_matches_container,
    failure_buckets,
    format_summary,
    memory_ratio_by_format,
    parse_status_for,
    percentile,
    result_matrix,
    sanitize_reason,
)
from document_processing.models import ParsedCell, ParsedDocument, ParsedParagraph, ParsedTable
from document_processing.parsers.exceptions import ERROR_CODES

from support import builders


class TestResultCodeTaxonomy:
    def test_matches_the_parser_error_taxonomy(self):
        # TEXT_EXTRACTED is the only code that is not an error code.
        assert set(RESULT_CODES) - {"TEXT_EXTRACTED"} == set(ERROR_CODES)

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("TEXT_EXTRACTED", "SUCCESS"),
            ("EMPTY_DOCUMENT", "SUCCESS"),
            ("OCR_REQUIRED", "SUCCESS"),
            ("ENCRYPTED", "SUCCESS"),
            ("CORRUPT", "SUCCESS"),
            ("UNSUPPORTED_FORMAT", "SUCCESS"),
            ("PARSE_FAILED", "FAILED"),
            (None, "PENDING"),
        ],
    )
    def test_parse_status_mapping(self, code, expected):
        # A determination the parser made on purpose is a worker success;
        # only an unattributable failure is a worker failure.
        assert parse_status_for(code) == expected

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("TEXT_EXTRACTED", "PENDING"),
            ("OCR_REQUIRED", "SKIPPED"),
            ("EMPTY_DOCUMENT", "SKIPPED"),
            ("ENCRYPTED", "SKIPPED"),
            (None, "PENDING"),
        ],
    )
    def test_downstream_status_mapping(self, code, expected):
        assert downstream_status_for(code) == expected


class TestPrivacy:
    def test_record_has_no_field_that_could_hold_a_path_or_text(self):
        fields = {f.name for f in dataclasses.fields(CorpusRecord)}
        forbidden = {"path", "file_path", "filename", "name", "text",
                     "extracted_text", "normalized_text", "source_path"}
        assert fields & forbidden == set()

    @pytest.mark.parametrize(
        "message",
        [
            "cannot open /internal/share/인사/2026 급여명세.hwp",
            "failed on 'C:\\\\share\\\\secret.hwpx' at offset 91821",
            "BodyText stream of \"계약서 최종본\" is truncated",
        ],
    )
    def test_sanitize_strips_paths_quotes_and_digits(self, message):
        bucket = sanitize_reason(message, None)
        assert "/" not in bucket and "\\" not in bucket
        assert "'" not in bucket and '"' not in bucket
        assert not any(ch.isdigit() for ch in bucket)

    def test_sanitize_prefers_the_detail_code(self):
        assert sanitize_reason("anything at all", "DISTRIBUTION_DOCUMENT") == (
            "DISTRIBUTION_DOCUMENT"
        )

    def test_sanitize_handles_missing_input(self):
        assert sanitize_reason(None, None) == "UNKNOWN"

    def test_sanitize_is_length_bounded(self):
        assert len(sanitize_reason("x" * 500, None)) <= 80

    def test_anonymous_ids_do_not_encode_the_filename(self, tmp_path):
        paths = [tmp_path / "2026_인사평가.hwp", tmp_path / "급여.hwpx"]
        ids = [aid for aid, _ in assign_anonymous_ids(paths)]
        assert ids == ["HWP-001", "HWPX-001"]


class TestAnonymousIds:
    def test_ids_are_stable_across_runs(self, tmp_path):
        paths = [tmp_path / f"doc{n}.hwpx" for n in range(5)]
        assert assign_anonymous_ids(paths) == assign_anonymous_ids(list(reversed(paths)))

    def test_ids_are_partitioned_by_format(self, tmp_path):
        paths = [tmp_path / "a.hwp", tmp_path / "b.hwpx", tmp_path / "c.hwp"]
        ids = sorted(aid for aid, _ in assign_anonymous_ids(paths))
        assert ids == ["HWP-001", "HWP-002", "HWPX-001"]

    def test_every_input_gets_exactly_one_id(self, tmp_path):
        paths = [tmp_path / f"d{n}.hwp" for n in range(30)]
        pairs = assign_anonymous_ids(paths)
        assert len(pairs) == 30
        assert len({aid for aid, _ in pairs}) == 30


class TestContainerDetection:
    def test_detects_zip(self, tmp_path):
        path = builders.write_hwpx(tmp_path / "a.hwpx")
        assert detect_container(path) == "zip"

    def test_detects_ole(self, tmp_path):
        path = builders.write_ole_magic_only(tmp_path / "a.hwp")
        assert detect_container(path) == "ole"

    def test_unknown_and_empty(self, tmp_path):
        (tmp_path / "junk.hwp").write_bytes(b"not a container")
        (tmp_path / "zero.hwp").write_bytes(b"")
        assert detect_container(tmp_path / "junk.hwp") == "unknown"
        assert detect_container(tmp_path / "zero.hwp") == "empty"

    def test_missing_file_does_not_raise(self, tmp_path):
        assert detect_container(tmp_path / "nope.hwp") == "unknown"

    @pytest.mark.parametrize(
        ("ext", "container", "expected"),
        [
            ("hwp", "ole", True),
            ("hwp", "zip", False),      # OI-6: HWPX saved as .hwp
            ("hwpx", "zip", True),
            ("hwpx", "ole", False),
            ("pdf", "unknown", None),
        ],
    )
    def test_extension_container_agreement(self, ext, container, expected):
        assert extension_matches_container(ext, container) is expected

    def test_mismatch_aggregation(self):
        records = [
            CorpusRecord("HWP-001", "hwp", 10, container="zip", extension_container_match=False),
            CorpusRecord("HWP-002", "hwp", 10, container="ole", extension_container_match=True),
        ]
        assert container_mismatches(records) == {"hwp -> zip": 1}


def make_doc(tables=(), paragraphs=2, warnings=()) -> ParsedDocument:
    doc = ParsedDocument(file_path="x", file_type="hwpx")
    doc.paragraphs = [ParsedParagraph(index=i, text=f"문단 {i}") for i in range(paragraphs)]
    doc.tables = list(tables)
    doc.warnings = list(warnings)
    return doc


def make_table(rows=2, cols=2, declared=None, anchor=0) -> ParsedTable:
    grid = [[f"r{r}c{c}" for c in range(cols)] for r in range(rows)]
    cells = [
        ParsedCell(row=r, column=c, text=grid[r][c])
        for r in range(rows) for c in range(cols)
    ]
    dr, dc = declared if declared else (rows, cols)
    return ParsedTable(
        index=0, rows=grid, cells=cells, paragraph_index=anchor,
        declared_row_count=dr, declared_column_count=dc,
    )


class TestTableCheck:
    def test_no_tables_passes(self):
        assert check_tables(make_doc())[0] == "PASS"

    def test_clean_table_passes(self):
        assert check_tables(make_doc(tables=[make_table()]))[0] == "PASS"

    def test_merged_cells_are_a_warning_not_a_failure(self):
        verdict, reasons = check_tables(
            make_doc(tables=[make_table()], warnings=["MERGED_CELLS_PRESENT"])
        )
        assert verdict == "PASS_WITH_WARNING"
        assert "MERGED_CELLS_PRESENT" in reasons

    def test_structure_loss_is_a_failure(self):
        verdict, _ = check_tables(
            make_doc(tables=[make_table()], warnings=["MERGED_CELL_STRUCTURE_LOSS"])
        )
        assert verdict == "FAIL"

    def test_grid_disagreeing_with_the_file_is_a_failure(self):
        verdict, reasons = check_tables(make_doc(tables=[make_table(declared=(5, 2))]))
        assert verdict == "FAIL"
        assert "ROW_COUNT_DISAGREES_WITH_FILE" in reasons

    def test_unanchored_table_is_flagged(self):
        verdict, reasons = check_tables(make_doc(tables=[make_table(anchor=None)]))
        assert verdict in {"FAIL", "PASS_WITH_WARNING"}
        assert "TABLE_NOT_ANCHORED_TO_PARAGRAPH" in reasons

    def test_failure_wins_over_warning(self):
        verdict, _ = check_tables(
            make_doc(
                tables=[make_table()],
                warnings=["MERGED_CELLS_PRESENT", "TABLE_ROW_COUNT_MISMATCH"],
            )
        )
        assert verdict == "FAIL"


class TestParagraphAnchors:
    def test_contiguous_indices_pass(self):
        assert check_paragraph_anchors(make_doc(paragraphs=5)) is True

    def test_gap_fails(self):
        doc = make_doc(paragraphs=3)
        doc.paragraphs[2].index = 7
        assert check_paragraph_anchors(doc) is False

    def test_empty_document_is_trivially_consistent(self):
        assert check_paragraph_anchors(make_doc(paragraphs=0)) is True


class TestStatistics:
    def test_percentile_of_empty_is_none(self):
        assert percentile([], 95) is None

    def test_percentile_of_single_value(self):
        assert percentile([4.0], 95) == 4.0

    def test_percentile_is_within_range(self):
        values = [float(v) for v in range(1, 101)]
        assert percentile(values, 50) <= percentile(values, 95) <= max(values)

    @pytest.mark.parametrize(
        ("size", "bucket"),
        [
            (1, "< 1 MB"),
            ((1 << 20) - 1, "< 1 MB"),
            (1 << 20, "1-5 MB"),
            (6 << 20, "5-20 MB"),
            (30 << 20, "20-50 MB"),
            (100 << 20, "> 50 MB"),
        ],
    )
    def test_size_buckets(self, size, bucket):
        assert bucket_for(size) == bucket


def record(**kw) -> CorpusRecord:
    base = dict(anonymous_id="X-001", format="hwpx", file_size_bytes=2 << 20)
    base.update(kw)
    return CorpusRecord(**base)


class TestAggregation:
    def test_result_matrix_splits_by_format(self):
        records = [
            record(anonymous_id="HWP-001", format="hwp", parse_result_code="TEXT_EXTRACTED"),
            record(anonymous_id="HWPX-001", parse_result_code="TEXT_EXTRACTED"),
            record(anonymous_id="HWPX-002", parse_result_code="ENCRYPTED"),
        ]
        matrix = result_matrix(records)
        assert matrix["TEXT_EXTRACTED"] == {"hwp": 1, "hwpx": 1, "total": 2}
        assert matrix["ENCRYPTED"]["total"] == 1
        assert matrix["CORRUPT"]["total"] == 0

    def test_format_summary_rates(self):
        records = [
            record(parse_result_code="TEXT_EXTRACTED", paragraph_anchor_available=True,
                   section_title_count=2, parse_time_ms=10.0, peak_rss_mb=30.0),
            record(parse_result_code="TEXT_EXTRACTED", paragraph_anchor_available=True,
                   section_title_count=0, parse_time_ms=20.0, peak_rss_mb=40.0),
            record(parse_result_code="OCR_REQUIRED"),
        ]
        summary = format_summary(records, "hwpx")
        assert summary["count"] == 3
        assert summary["text_extracted"] == 2
        assert summary["paragraph_anchor_rate"] == 1.0
        assert summary["section_title_rate"] == 0.5

    def test_empty_corpus_summary_does_not_divide_by_zero(self):
        assert format_summary([], "hwp") == {"count": 0}

    def test_failure_buckets_exclude_successes(self):
        records = [
            record(parse_result_code="TEXT_EXTRACTED", failure_bucket=None),
            record(parse_result_code="CORRUPT", failure_bucket="BadZipFile"),
            record(parse_result_code="CORRUPT", failure_bucket="BadZipFile"),
            record(parse_result_code="PARSE_FAILED", failure_bucket="TIMEOUT"),
        ]
        assert failure_buckets(records) == {
            "CORRUPT": {"BadZipFile": 2},
            "PARSE_FAILED": {"TIMEOUT": 1},
        }

    def test_memory_ratio_excludes_small_files(self):
        # A tiny file's RSS is dominated by interpreter baseline, so including
        # it would inflate the ratio into nonsense.
        records = [
            record(file_size_bytes=1024, parse_result_code="TEXT_EXTRACTED",
                   rss_over_baseline_mb=0.4),
            record(file_size_bytes=4 << 20, parse_result_code="TEXT_EXTRACTED",
                   rss_over_baseline_mb=40.0),
        ]
        stats = memory_ratio_by_format(records)["hwpx"]
        assert stats["count"] == 1
        assert stats["mean"] == pytest.approx(10.0)

    def test_memory_ratio_reports_its_own_caveat(self):
        stats = memory_ratio_by_format([record(parse_result_code="TEXT_EXTRACTED")])["hwpx"]
        assert str(RATIO_MIN_FILE_BYTES // (1 << 20)) in stats["note"]
