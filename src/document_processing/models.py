"""Structured representation of a parsed document.

The models here are intentionally parser-agnostic: they describe *what was found
in the document*, never what a particular library happens to expose.  A field
that a parser cannot determine from the file itself is left as ``None`` -- it is
never inferred, estimated or back-filled.  See ``docs/poc/hwp-poc-report.md``
section 6 for why that matters for citation anchors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Paragraph kinds
#
# Only values we can justify from document data are used.  "heading" is emitted
# solely when the document's own style definition says so (HWP DocInfo styles /
# HWPX <hh:style>), never from font size or text heuristics.  "textbox" marks
# text laid out inside a drawing object rather than the main text flow.
# ---------------------------------------------------------------------------
ParagraphType = Literal["body", "heading", "textbox", "table_cell", "caption"]


@dataclass(frozen=True)
class ParsedCell:
    """One table cell, with the address the document itself declares."""

    row: int
    column: int
    text: str

    # Span values come from the file (HWP cell list header / HWPX <hp:cellSpan>).
    row_span: int = 1
    column_span: int = 1

    @property
    def is_merged(self) -> bool:
        return self.row_span > 1 or self.column_span > 1


@dataclass
class ParsedParagraph:
    """A body paragraph in document order.

    ``index`` is the document-wide, zero-based paragraph ordinal and is the one
    anchor this PoC guarantees for every paragraph of every successfully parsed
    document.
    """

    index: int
    text: str

    page_number: int | None = None
    section_title: str | None = None
    paragraph_type: ParagraphType | None = None

    # Zero-based index of the HWP section / HWPX section?.xml the paragraph came
    # from.  This is a *document section*, not a printed page.
    section_index: int | None = None

    # Style name as declared by the document, when available.  Kept for
    # provenance/debugging; not used to guess headings beyond the explicit
    # outline styles the parsers recognise.
    style_name: str | None = None


@dataclass
class ParsedTable:
    """A table with its cell grid preserved in row/column order."""

    index: int
    rows: list[list[str]]
    cells: list[ParsedCell] = field(default_factory=list)

    page_number: int | None = None

    # Index of the paragraph the table is anchored to, so body/table interleaving
    # order can be reconstructed.
    paragraph_index: int | None = None

    section_index: int | None = None

    # Declared grid size from the file, kept separately from ``rows`` so a
    # mismatch between the declaration and what we recovered stays visible.
    declared_row_count: int | None = None
    declared_column_count: int | None = None

    has_merged_cells: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(r) for r in self.rows), default=0)


@dataclass
class ParsedDocument:
    """The full structured parse result.

    ``paragraphs`` and ``tables`` are the structured representation.  The
    searchable flat text is produced separately by
    :func:`document_processing.normalize.build_normalized_text` so that the
    structure is never lost to normalisation.
    """

    file_path: str
    file_type: str

    paragraphs: list[ParsedParagraph] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)

    metadata: dict[str, Any] = field(default_factory=dict)

    page_count: int | None = None

    parser_name: str = ""
    parser_version: str = ""

    warnings: list[str] = field(default_factory=list)

    # ---- convenience -----------------------------------------------------

    @property
    def paragraph_count(self) -> int:
        return len(self.paragraphs)

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def text_length(self) -> int:
        return sum(len(p.text) for p in self.paragraphs)

    def add_warning(self, code: str) -> None:
        """Record a warning code once.

        Warnings are stable, greppable codes (not sentences) so that ingestion
        can aggregate them across a corpus.
        """
        if code not in self.warnings:
            self.warnings.append(code)

    def has_page_anchors(self) -> bool:
        return any(p.page_number is not None for p in self.paragraphs)

    def has_section_titles(self) -> bool:
        return any(p.section_title for p in self.paragraphs)
