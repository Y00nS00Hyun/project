"""DOCX parser: text extraction, reading order, and failure classification.

Fixtures are built with python-docx rather than checked in as binaries, so the
expected content is visible next to the assertion and no real internal document
is committed to the repository.
"""

from __future__ import annotations

import zipfile

import pytest

pytest.importorskip("docx", reason="python-docx is required for the DOCX parser")

import docx  # noqa: E402

from document_processing.normalize import build_normalized_text  # noqa: E402
from document_processing.parsers import get_parser, parse_document  # noqa: E402
from document_processing.parsers.docx import DocxParser  # noqa: E402
from document_processing.parsers.exceptions import (  # noqa: E402
    CorruptDocumentError,
    DocumentParseError,
    EmptyDocumentError,
)


def write(tmp_path, name="sample.docx", build=None):
    document = docx.Document()
    if build is not None:
        build(document)
    path = tmp_path / name
    document.save(str(path))
    return path


class TestRegistration:
    def test_the_registry_claims_docx(self, tmp_path):
        parser = get_parser(write(tmp_path, build=lambda d: d.add_paragraph("본문")))
        assert isinstance(parser, DocxParser)

    def test_docm_is_not_claimed(self):
        # Macro-enabled documents are the same XML plus code, and out of scope.
        assert DocxParser().supports(__import__("pathlib").Path("a.docm")) is False


class TestTextExtraction:
    def test_plain_paragraphs_are_extracted(self, tmp_path):
        def build(d):
            d.add_paragraph("First paragraph.")
            d.add_paragraph("Second paragraph.")

        parsed = parse_document(write(tmp_path, build=build))
        assert [p.text for p in parsed.paragraphs] == [
            "First paragraph.", "Second paragraph.",
        ]
        assert parsed.file_type == "docx"
        assert parsed.parser_name == "inhouse-docx"

    def test_korean_text_survives_intact(self, tmp_path):
        body = "2026년도 사업계획서 — 예산 3억 원, 담당: 기획조정실"

        def build(d):
            d.add_paragraph(body)

        parsed = parse_document(write(tmp_path, build=build))
        assert parsed.paragraphs[0].text == body
        assert body in build_normalized_text(parsed)

    def test_a_heading_is_marked_from_the_documents_own_style(self, tmp_path):
        def build(d):
            d.add_heading("제 1 장 총칙", level=1)
            d.add_paragraph("본문입니다.")

        parsed = parse_document(write(tmp_path, build=build))
        kinds = {p.text: p.paragraph_type for p in parsed.paragraphs}
        assert kinds["제 1 장 총칙"] == "heading"
        # Never guessed from length or position.
        assert kinds["본문입니다."] == "body"

    def test_blank_spacing_paragraphs_do_not_consume_anchors(self, tmp_path):
        def build(d):
            d.add_paragraph("A")
            d.add_paragraph("")
            d.add_paragraph("   ")
            d.add_paragraph("B")

        parsed = parse_document(write(tmp_path, build=build))
        # Indexes are citation anchors; empty spacing runs must not shift them.
        assert [(p.index, p.text) for p in parsed.paragraphs] == [(0, "A"), (1, "B")]


class TestReadingOrder:
    """The reason this parser walks the body instead of using python-docx's
    two flat lists: `paragraphs` then `tables` reorders every interleaved
    document, and a citation would then point at text that is not there."""

    @staticmethod
    def build_interleaved(d):
        d.add_paragraph("문단 A")
        table = d.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "표 왼쪽"
        table.rows[0].cells[1].text = "표 오른쪽"
        d.add_paragraph("문단 B")

    def test_paragraph_table_paragraph_keeps_its_order(self, tmp_path):
        parsed = parse_document(write(tmp_path, build=self.build_interleaved))
        text = build_normalized_text(parsed)
        assert text.index("문단 A") < text.index("표 왼쪽")
        assert text.index("표 왼쪽") < text.index("문단 B")

    def test_the_table_is_anchored_to_the_paragraph_before_it(self, tmp_path):
        parsed = parse_document(write(tmp_path, build=self.build_interleaved))
        first = next(p for p in parsed.paragraphs if p.text == "문단 A")
        assert parsed.tables[0].paragraph_index == first.index

    def test_naive_extraction_would_have_failed_this(self, tmp_path):
        """Pins the bug this parser exists to avoid.

        python-docx's own flat lists put every table after every paragraph. If
        the walk ever regresses to that, the assertion above still passes for a
        table-last document -- this one would not.
        """
        path = write(tmp_path, build=self.build_interleaved)
        source = docx.Document(str(path))
        naive = [p.text for p in source.paragraphs if p.text.strip()]
        naive += [c.text for t in source.tables for r in t.rows for c in r.cells]
        assert naive.index("문단 B") < naive.index("표 왼쪽")

        ours = build_normalized_text(parse_document(path))
        assert ours.index("표 왼쪽") < ours.index("문단 B")


class TestTables:
    def test_cell_text_reaches_the_searchable_output(self, tmp_path):
        def build(d):
            d.add_paragraph("예산 내역")
            table = d.add_table(rows=2, cols=2)
            table.rows[0].cells[0].text = "항목"
            table.rows[0].cells[1].text = "금액"
            table.rows[1].cells[0].text = "인건비"
            table.rows[1].cells[1].text = "1억 2천만원"

        parsed = parse_document(write(tmp_path, build=build))
        text = build_normalized_text(parsed)
        for cell in ("항목", "금액", "인건비", "1억 2천만원"):
            assert cell in text, cell

    def test_the_grid_keeps_row_and_column_addresses(self, tmp_path):
        def build(d):
            table = d.add_table(rows=2, cols=2)
            table.rows[0].cells[0].text = "r0c0"
            table.rows[0].cells[1].text = "r0c1"
            table.rows[1].cells[0].text = "r1c0"
            table.rows[1].cells[1].text = "r1c1"

        parsed = parse_document(write(tmp_path, build=build))
        table = parsed.tables[0]
        assert table.rows == [["r0c0", "r0c1"], ["r1c0", "r1c1"]]
        assert {(c.row, c.column, c.text) for c in table.cells} == {
            (0, 0, "r0c0"), (0, 1, "r0c1"), (1, 0, "r1c0"), (1, 1, "r1c1"),
        }

    def test_a_merged_cell_contributes_its_text_once(self, tmp_path):
        def build(d):
            table = d.add_table(rows=1, cols=2)
            merged = table.rows[0].cells[0].merge(table.rows[0].cells[1])
            merged.text = "병합된 제목"

        parsed = parse_document(write(tmp_path, build=build))
        table = parsed.tables[0]
        # python-docx repeats the same cell object across the span; counting it
        # twice would duplicate the text in every chunk built from the table.
        assert [c.text for c in table.cells] == ["병합된 제목"]
        assert table.has_merged_cells is True
        assert build_normalized_text(parsed).count("병합된 제목") == 1

    def test_a_leading_table_is_not_lost(self, tmp_path):
        def build(d):
            table = d.add_table(rows=1, cols=1)
            table.rows[0].cells[0].text = "첫 줄이 표"
            d.add_paragraph("뒤따르는 문단")

        parsed = parse_document(write(tmp_path, build=build))
        # No preceding paragraph to anchor to, exactly as in the HWPX parser.
        # build_normalized_text appends such a table rather than dropping it.
        assert parsed.tables[0].paragraph_index is None
        assert "첫 줄이 표" in build_normalized_text(parsed)


class TestFailures:
    def test_a_file_that_is_not_a_docx_is_classified_not_crashed(self, tmp_path):
        path = tmp_path / "fake.docx"
        path.write_bytes(b"this is not a zip at all")

        with pytest.raises(CorruptDocumentError) as caught:
            parse_document(path)
        assert caught.value.error_code == "CORRUPT"

    def test_a_truncated_package_is_classified(self, tmp_path):
        good = write(tmp_path, build=lambda d: d.add_paragraph("본문"))
        broken = tmp_path / "broken.docx"
        broken.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 3])

        with pytest.raises(DocumentParseError) as caught:
            parse_document(broken)
        # Either classification is honest; what matters is that it is a
        # determination and not an escaping library exception.
        assert caught.value.error_code in {"CORRUPT", "PARSE_FAILED"}

    def test_a_zip_that_is_not_a_word_document_is_classified(self, tmp_path):
        path = tmp_path / "spreadsheet.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")

        with pytest.raises(DocumentParseError):
            parse_document(path)

    def test_an_empty_document_is_empty_not_failed(self, tmp_path):
        with pytest.raises(EmptyDocumentError) as caught:
            parse_document(write(tmp_path))
        assert caught.value.error_code == "EMPTY_DOCUMENT"

    def test_every_failure_is_a_parse_error_subclass(self, tmp_path):
        """What keeps one bad file from killing an ingestion run.

        The worker catches DocumentParseError and records it against the
        revision; anything else would escape as an arbitrary library exception.
        """
        path = tmp_path / "nonsense.docx"
        path.write_bytes(b"\x00\x01\x02")
        with pytest.raises(DocumentParseError):
            parse_document(path)
