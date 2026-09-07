"""Unit tests for normalization, canonicalization and hashing."""

from __future__ import annotations

import unicodedata

from document_processing.models import ParsedCell, ParsedDocument, ParsedParagraph, ParsedTable
from document_processing.normalize import (
    build_normalized_text,
    canonical_json,
    canonicalize,
    content_hash,
    normalize_text,
    normalized_text_hash,
)


def make_doc() -> ParsedDocument:
    doc = ParsedDocument(
        file_path="/tmp/example.hwpx",
        file_type="hwpx",
        parser_name="inhouse-hwpx",
        parser_version="0.1.0",
    )
    doc.paragraphs = [
        ParsedParagraph(index=0, text="2026년 사업계획", paragraph_type="heading"),
        ParsedParagraph(index=1, text="2026년 사업계획은 다음과 같다.", paragraph_type="body"),
        ParsedParagraph(index=2, text="끝.", paragraph_type="body"),
    ]
    doc.tables = [
        ParsedTable(
            index=0,
            rows=[["부서", "예산", "담당자"], ["기획실", "300000000", "홍길동"]],
            cells=[
                ParsedCell(row=0, column=0, text="부서"),
                ParsedCell(row=0, column=1, text="예산"),
                ParsedCell(row=0, column=2, text="담당자"),
                ParsedCell(row=1, column=0, text="기획실"),
                ParsedCell(row=1, column=1, text="300000000"),
                ParsedCell(row=1, column=2, text="홍길동"),
            ],
            paragraph_index=1,
            declared_row_count=2,
            declared_column_count=3,
        )
    ]
    return doc


class TestNormalizeText:
    def test_collapses_whitespace_without_dropping_characters(self):
        assert normalize_text("  가   나\t다  ") == "가 나 다"

    def test_preserves_korean_digits_and_punctuation(self):
        raw = "제1조(목적) 예산 300,000,000원 — 담당자: 홍길동 (기획실)"
        assert normalize_text(raw) == raw

    def test_composes_decomposed_hangul(self):
        decomposed = unicodedata.normalize("NFD", "한글")
        assert decomposed != "한글"
        assert normalize_text(decomposed) == "한글"

    def test_folds_special_spaces(self):
        # non-breaking, fixed-width and ideographic spaces
        assert normalize_text("가 나　다") == "가 나 다"

    def test_does_not_change_case(self):
        assert normalize_text("Annual Report 2026") == "Annual Report 2026"

    def test_empty(self):
        assert normalize_text("") == ""


class TestBuildNormalizedText:
    def test_table_is_rendered_at_its_anchor_paragraph(self):
        text = build_normalized_text(make_doc())
        lines = text.splitlines()
        assert lines == [
            "[문단]",
            "2026년 사업계획",
            "[문단]",
            "2026년 사업계획은 다음과 같다.",
            "[표]",
            "부서 | 예산 | 담당자",
            "기획실 | 300000000 | 홍길동",
            "[문단]",
            "끝.",
        ]

    def test_unanchored_table_is_appended_not_dropped(self):
        doc = make_doc()
        doc.tables[0].paragraph_index = None
        text = build_normalized_text(doc)
        assert "부서 | 예산 | 담당자" in text
        assert text.rstrip().endswith("기획실 | 300000000 | 홍길동")

    def test_table_anchored_to_missing_paragraph_is_kept(self):
        doc = make_doc()
        doc.tables[0].paragraph_index = 999
        assert "기획실 | 300000000 | 홍길동" in build_normalized_text(doc)

    def test_empty_paragraphs_are_skipped(self):
        doc = make_doc()
        doc.paragraphs.append(ParsedParagraph(index=3, text="\n"))
        assert build_normalized_text(doc).count("[문단]") == 3


class TestCanonicalization:
    def test_hash_is_independent_of_file_path(self):
        a, b = make_doc(), make_doc()
        b.file_path = "/completely/different/location.hwpx"
        assert content_hash(a) == content_hash(b)

    def test_hash_is_independent_of_warning_order(self):
        a, b = make_doc(), make_doc()
        a.warnings = ["B_CODE", "A_CODE"]
        b.warnings = ["A_CODE", "B_CODE"]
        assert content_hash(a) == content_hash(b)

    def test_hash_changes_when_text_changes(self):
        a, b = make_doc(), make_doc()
        b.paragraphs[1].text += " (개정)"
        assert content_hash(a) != content_hash(b)

    def test_hash_changes_when_a_table_cell_changes(self):
        a, b = make_doc(), make_doc()
        b.tables[0].rows[1][1] = "400000000"
        assert content_hash(a) != content_hash(b)

    def test_hash_is_stable_across_repeated_calls(self):
        doc = make_doc()
        assert len({content_hash(doc) for _ in range(5)}) == 1

    def test_non_primitive_metadata_cannot_affect_the_hash(self):
        a, b = make_doc(), make_doc()
        b.metadata["transient"] = object()
        assert content_hash(a) == content_hash(b)

    def test_canonical_json_is_key_sorted_and_keeps_korean_readable(self):
        payload = canonical_json(make_doc())
        assert '"file_type":"hwpx"' in payload
        assert "2026년 사업계획" in payload

    def test_canonical_form_excludes_file_path(self):
        assert "file_path" not in canonicalize(make_doc())

    def test_normalized_text_hash_tracks_normalized_text_only(self):
        a, b = make_doc(), make_doc()
        b.metadata["title"] = "irrelevant to the flat text"
        assert normalized_text_hash(a) == normalized_text_hash(b)
        assert content_hash(a) != content_hash(b)
