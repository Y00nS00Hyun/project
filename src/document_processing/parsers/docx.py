"""DOCX (Office Open XML WordprocessingML) parser.

Built on ``python-docx``, which owns the OPC package handling and the element
wrappers.  What this file adds is the part python-docx deliberately does not
do: reading the body in document order.

``Document.paragraphs`` and ``Document.tables`` are two separate flat lists, so
the obvious implementation -- all paragraphs, then all tables -- silently
reorders every document that interleaves them.  A report whose table sits
between two paragraphs would be indexed with that table at the end, and a
search hit would cite text that does not appear where the citation says it
does.  So the walk below iterates ``<w:body>``'s children and keeps the order
the file states.

Scope is searchable text: paragraphs, headings and table cells.  Images are
counted (so a scanned document is classified as OCR_REQUIRED rather than
empty) but never interpreted, and drawing/textbox content, headers, footers,
comments and tracked-change history are out of scope for this parser.

As with HWP and HWPX, no page numbers are produced.  Word stores no page index
in the file -- pagination is a rendering result, and the layout cache Word
keeps is not a source we would cite from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from ..models import ParsedCell, ParsedDocument, ParsedParagraph, ParsedTable
from .base import BaseParser
from .exceptions import CorruptDocumentError, ParseFailedError

PARSER_NAME = "inhouse-docx"
PARSER_VERSION = "0.1.0"

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Built-in style ids for outline headings. Word localises style *names*
#: ("제목 1" in a Korean install), but the built-in style *id* stays English, so
#: the id is what we match on. Same rule as the HWP parsers: a heading is a
#: heading because the document's style says so, never because of font size.
_HEADING_STYLE_IDS = ("Heading", "Title", "Subtitle")

#: Relationship type suffix for an embedded image part.
_IMAGE_RELTYPE = "/image"


def _tag(element: Any) -> str:
    """Local tag name of an lxml element, namespace stripped."""
    tag = element.tag
    if isinstance(tag, str) and tag.startswith(W_NS):
        return tag[len(W_NS):]
    return str(tag)


class DocxParser(BaseParser):
    """Parses DOCX documents into a :class:`ParsedDocument`."""

    name = PARSER_NAME
    version = PARSER_VERSION
    # .docm is deliberately absent: a macro-enabled document is the same XML
    # plus code we have no reason to open from a shared folder.
    extensions = (".docx",)

    def _parse(self, file_path: Path) -> ParsedDocument:
        try:
            import docx
            from docx.opc.exceptions import OpcError, PackageNotFoundError
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ParseFailedError(
                "python-docx is not installed", detail="MISSING_DEPENDENCY"
            ) from exc

        try:
            source = docx.Document(str(file_path))
        except PackageNotFoundError as exc:
            # Not a readable OPC package: a renamed file, a truncated download,
            # or the old binary .doc format wearing a .docx extension.
            raise CorruptDocumentError(
                f"not a readable DOCX package: {exc}", detail="PACKAGE_NOT_FOUND"
            ) from exc
        except OpcError as exc:
            raise CorruptDocumentError(str(exc), detail=type(exc).__name__) from exc
        except ValueError as exc:
            # python-docx raises this for a package that opens but is not a
            # WordprocessingML document (an .xlsx renamed to .docx, say).
            raise CorruptDocumentError(str(exc), detail="NOT_A_WORD_DOCUMENT") from exc

        doc = ParsedDocument(
            file_path=str(file_path),
            file_type="docx",
            parser_name=self.name,
            parser_version=self.version,
        )
        self._read_metadata(source, doc)
        self._walk_body(source, doc)
        doc.metadata["image_count"] = self._count_images(source)
        return doc

    # -- body ---------------------------------------------------------------

    def _walk_body(self, source: Any, doc: ParsedDocument) -> None:
        """Emit paragraphs and tables in the order ``<w:body>`` declares them."""
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        body = source.element.body
        for child in body.iterchildren():
            kind = _tag(child)
            if kind == "p":
                self._emit_paragraph(Paragraph(child, source), doc)
            elif kind == "tbl":
                self._emit_table(Table(child, source), doc)
            # Everything else in a body -- sectPr, bookmarks, sdt wrappers --
            # carries no body text of its own. Content inside a structured
            # document tag (sdt) is the one real omission; Word writes those
            # for content controls, which the corpus does not use.

    def _emit_paragraph(self, paragraph: Any, doc: ParsedDocument) -> None:
        text = paragraph.text
        if not text.strip():
            # Word writes empty paragraphs for vertical spacing. Keeping them
            # would inflate every paragraph index without adding a citable
            # anchor, and build_normalized_text drops them anyway.
            return
        doc.paragraphs.append(
            ParsedParagraph(
                index=len(doc.paragraphs),
                text=text,
                page_number=None,
                paragraph_type="heading" if self._is_heading(paragraph) else "body",
                style_name=self._style_name(paragraph),
            )
        )

    @staticmethod
    def _style_name(paragraph: Any) -> str | None:
        try:
            return paragraph.style.name if paragraph.style is not None else None
        except Exception:
            # A document referencing a style id that styles.xml does not define.
            return None

    @staticmethod
    def _is_heading(paragraph: Any) -> bool:
        """A heading only when the document's own style says so."""
        try:
            style = paragraph.style
            style_id = getattr(style, "style_id", None) if style is not None else None
        except Exception:
            return False
        if not style_id:
            return False
        return style_id.startswith(_HEADING_STYLE_IDS)

    # -- tables -------------------------------------------------------------

    def _emit_table(self, table: Any, doc: ParsedDocument) -> None:
        """Flatten one table into a row/column grid of cell text.

        Anchored to the paragraph that precedes it, which is the convention the
        HWPX parser uses and what ``build_normalized_text`` renders from: the
        table's text is emitted right after that paragraph, so reading order
        survives into the searchable text and into every chunk built from it.
        """
        anchor = len(doc.paragraphs) - 1 if doc.paragraphs else None

        cells: list[ParsedCell] = []
        rows: list[list[str]] = []
        has_merged = False
        # Identity of the <w:tc> already consumed, per grid position, so a
        # merged cell contributes its text once instead of once per column it
        # spans -- python-docx repeats the same cell object across a span.
        seen: dict[int, tuple[int, int]] = {}

        for row_index, row in enumerate(self._rows(table, doc)):
            row_text: list[str] = []
            for column_index, cell in enumerate(row):
                element_id = id(cell._tc)
                previous = seen.get(element_id)
                if previous is not None:
                    has_merged = True
                    origin_row, origin_column = previous
                    for existing in cells:
                        if existing.row == origin_row and existing.column == origin_column:
                            cells[cells.index(existing)] = ParsedCell(
                                row=existing.row,
                                column=existing.column,
                                text=existing.text,
                                row_span=max(existing.row_span, row_index - origin_row + 1),
                                column_span=max(
                                    existing.column_span, column_index - origin_column + 1
                                ),
                            )
                            break
                    # Repeated in the grid so the shape stays rectangular, but
                    # not counted twice in the flat text.
                    row_text.append("")
                    continue

                seen[element_id] = (row_index, column_index)
                text = self._cell_text(cell, doc)
                row_text.append(text)
                cells.append(ParsedCell(row=row_index, column=column_index, text=text))
            rows.append(row_text)

        parsed = ParsedTable(
            index=len(doc.tables),
            rows=rows,
            cells=cells,
            page_number=None,
            paragraph_index=anchor,
            declared_row_count=len(rows),
            declared_column_count=max((len(r) for r in rows), default=0),
            has_merged_cells=has_merged,
        )
        doc.tables.append(parsed)

    @staticmethod
    def _rows(table: Any, doc: ParsedDocument) -> Iterator[list[Any]]:
        """Cell objects per row, tolerating a malformed grid.

        python-docx raises when a row's gridSpan does not add up to the table's
        declared column count. One bad table must not cost us the rest of the
        document, so the row is recorded as lost and the walk continues.
        """
        for row in table.rows:
            try:
                yield list(row.cells)
            except Exception:
                doc.add_warning("TABLE_ROW_UNREADABLE")
                yield []

    def _cell_text(self, cell: Any, doc: ParsedDocument) -> str:
        """All text in a cell, including any table nested inside it.

        Nested tables are flattened into the cell rather than emitted as their
        own table, matching the HWPX parser. The warning records that the inner
        structure was not preserved -- the text is all still searchable.
        """
        if cell.tables:
            doc.add_warning("NESTED_TABLE_FLATTENED")
        parts = [p.text for p in cell.paragraphs if p.text.strip()]
        for nested in cell.tables:
            for row in self._rows(nested, doc):
                for inner in row:
                    inner_text = self._cell_text(inner, doc)
                    if inner_text:
                        parts.append(inner_text)
        return "\n".join(parts)

    # -- metadata -----------------------------------------------------------

    @staticmethod
    def _read_metadata(source: Any, doc: ParsedDocument) -> None:
        """Core properties. Non-fatal: a document without them still parses.

        Recorded for provenance only. The document's year and date come from
        the extracted text and the file name, exactly as for HWP and HWPX --
        a DOCX gets no date rule of its own.
        """
        try:
            properties = source.core_properties
        except Exception:
            doc.add_warning("METADATA_UNREADABLE")
            return
        for key, value in (
            ("title", getattr(properties, "title", None)),
            ("author", getattr(properties, "author", None)),
            ("language", getattr(properties, "language", None)),
        ):
            if isinstance(value, str) and value.strip():
                doc.metadata[key] = value.strip()

    @staticmethod
    def _count_images(source: Any) -> int:
        """Embedded image parts.

        Feeds BaseParser's empty/OCR classification: no text plus images means
        OCR_REQUIRED rather than EMPTY_DOCUMENT. Counts image *parts*, so a
        picture placed twice counts once -- the question being answered is
        whether the file carries pictures at all.
        """
        try:
            return sum(
                1 for rel in source.part.rels.values()
                if rel.reltype.endswith(_IMAGE_RELTYPE)
            )
        except Exception:
            return 0
