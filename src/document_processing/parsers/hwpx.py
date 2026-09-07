"""HWPX (OWPML) parser.

HWPX is an OPC-style ZIP of XML parts, so this parser needs no third-party
dependency at all -- only :mod:`zipfile` and :mod:`xml.etree.ElementTree` from
the standard library.  That is the main reason HWPX is the lower-risk of the
two formats for production ingestion (see docs/poc/hwp-poc-report.md).

As with the HWP parser, page numbers are never produced: OWPML stores a line
layout cache (``<hp:lineseg>``) but no page index, and the corpus measurement in
the report shows the page-first-line flag is not populated.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..models import ParsedCell, ParsedDocument, ParsedParagraph, ParsedTable
from .base import BaseParser
from .exceptions import (
    CorruptDocumentError,
    EncryptedDocumentError,
    ParseFailedError,
    UnsupportedFormatError,
)

PARSER_NAME = "inhouse-hwpx"
PARSER_VERSION = "0.1.0"

NS_PARA = "http://www.hancom.co.kr/hwpml/2011/paragraph"
NS_HEAD = "http://www.hancom.co.kr/hwpml/2011/head"

HWPX_MIMETYPE = b"application/hwp+zip"
SECTION_RE = re.compile(r"^Contents/section(\d+)\.xml$")

#: Refuse XML that declares a DTD.  ElementTree's expat backend will happily
#: expand internal entities, which is a denial-of-service vector on files that
#: arrive from a shared folder.  No legitimate HWPX declares one (verified
#: across the PoC corpus).
_DOCTYPE_SCAN_BYTES = 8192

#: Guard against zip bombs: a single decompressed XML part above this size is
#: rejected rather than loaded into the worker's memory.
MAX_PART_BYTES = 256 * 1024 * 1024

_HEADING_STYLE_PREFIXES = ("개요", "제목", "Outline", "Heading", "Title")

# Inline markers that carry meaning but no character data of their own.
_INLINE_TEXT = {"tab": "\t", "nbSpace": " ", "fwSpace": " ", "lineBreak": "\n"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _is_heading_style(name: str, eng_name: str) -> bool:
    for candidate in (name, eng_name):
        c = (candidate or "").strip()
        if c and any(c.startswith(p) for p in _HEADING_STYLE_PREFIXES):
            return True
    return False


def _text_of_t(node: ET.Element) -> str:
    """Flatten one ``<hp:t>`` node, honouring its inline markers."""
    parts = [node.text or ""]
    for child in node:
        tag = _local(child.tag)
        if tag in _INLINE_TEXT:
            parts.append(_INLINE_TEXT[tag])
        else:
            # Markers such as markpenBegin/insertBegin carry no text of their
            # own; itertext() keeps anything unexpected instead of dropping it.
            parts.append("".join(child.itertext()))
        parts.append(child.tail or "")
    return "".join(parts)


@dataclass
class _Context:
    """Where in the document the walker currently is."""

    section_index: int
    #: When set, paragraph text is collected into the current table cell
    #: instead of the document body.
    cell_sink: list[str] | None = None
    in_shape: bool = False
    section_title: str | None = None
    styles: dict[str, tuple[str, str]] = field(default_factory=dict)


class HwpxParser(BaseParser):
    """Parses HWPX documents into a :class:`ParsedDocument`."""

    name = PARSER_NAME
    version = PARSER_VERSION
    extensions = (".hwpx",)

    # -- entry point -------------------------------------------------------

    def _parse(self, file_path: Path) -> ParsedDocument:
        if not zipfile.is_zipfile(file_path):
            head = file_path.read_bytes()[:8]
            if head.startswith(b"\xd0\xcf\x11\xe0"):
                # The mirror image of the HWP parser's check.
                raise UnsupportedFormatError(
                    "file is an OLE compound file, not a ZIP container; "
                    "it is most likely HWP 5.x with a .hwpx extension"
                )
            raise CorruptDocumentError("not a ZIP container")

        try:
            with zipfile.ZipFile(file_path) as package:
                return self._parse_package(package, file_path)
        except zipfile.BadZipFile as exc:
            raise CorruptDocumentError(f"damaged ZIP container: {exc}") from exc

    # -- package -----------------------------------------------------------

    def _parse_package(self, package: zipfile.ZipFile, file_path: Path) -> ParsedDocument:
        names = package.namelist()

        broken = package.testzip()
        if broken is not None:
            raise CorruptDocumentError(f"CRC check failed for part '{broken}'")

        if "mimetype" in names:
            mimetype = package.read("mimetype").strip()
            if mimetype and mimetype != HWPX_MIMETYPE:
                raise UnsupportedFormatError(
                    f"unexpected mimetype {mimetype!r}, expected {HWPX_MIMETYPE!r}"
                )

        if self._is_encrypted(package, names):
            raise EncryptedDocumentError(
                "package manifest declares encrypted content",
                detail="MANIFEST_ENCRYPTION",
            )

        section_names = sorted(
            (n for n in names if SECTION_RE.match(n)),
            key=lambda n: int(SECTION_RE.match(n).group(1)),
        )
        if not section_names:
            raise CorruptDocumentError("no Contents/section*.xml parts found")

        doc = ParsedDocument(
            file_path=str(file_path),
            file_type="hwpx",
            parser_name=self.name,
            parser_version=self.version,
        )
        doc.page_count = None  # OWPML records no page count.
        doc.metadata["section_count"] = len(section_names)
        doc.metadata["image_count"] = sum(
            1 for n in names if n.startswith("BinData/") or n.startswith("Contents/BinData/")
        )

        self._read_metadata(package, names, doc)
        styles = self._read_styles(package, names, doc)

        for section_index, part in enumerate(section_names):
            root = self._load_xml(package, part)
            ctx = _Context(section_index=section_index, styles=styles)
            self._walk(root, ctx, doc)

        doc.add_warning("PAGE_NUMBER_NOT_AVAILABLE")
        if not doc.has_section_titles():
            doc.add_warning("SECTION_TITLE_NOT_AVAILABLE")
        return doc

    @staticmethod
    def _is_encrypted(package: zipfile.ZipFile, names: list[str]) -> bool:
        # ZIP-level password protection.
        if any(info.flag_bits & 0x1 for info in package.infolist()):
            return True
        if "META-INF/manifest.xml" in names:
            try:
                manifest = package.read("META-INF/manifest.xml")
            except Exception:
                return False
            if b"encryption-data" in manifest or b"<odf:encryption" in manifest:
                return True
        return False

    def _load_xml(self, package: zipfile.ZipFile, part: str) -> ET.Element:
        info = package.getinfo(part)
        if info.file_size > MAX_PART_BYTES:
            raise ParseFailedError(
                f"part '{part}' decompresses to {info.file_size} bytes, "
                f"above the {MAX_PART_BYTES} byte limit",
                detail="PART_TOO_LARGE",
            )
        data = package.read(part)
        if b"<!DOCTYPE" in data[:_DOCTYPE_SCAN_BYTES]:
            raise ParseFailedError(
                f"part '{part}' declares a DTD, which is refused",
                detail="XML_DTD_REJECTED",
            )
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise CorruptDocumentError(f"part '{part}' is not well-formed XML: {exc}") from exc

    def _read_metadata(self, package: zipfile.ZipFile, names: list[str], doc: ParsedDocument) -> None:
        """Pull document properties from Contents/content.hpf.

        Non-fatal: a package without readable metadata still parses.
        """
        if "Contents/content.hpf" not in names:
            return
        try:
            root = self._load_xml(package, "Contents/content.hpf")
        except Exception:
            doc.add_warning("METADATA_UNREADABLE")
            return
        for element in root.iter():
            tag = _local(element.tag)
            value = (element.text or "").strip()
            if tag == "title" and value:
                doc.metadata["title"] = value
            elif tag == "language" and value:
                doc.metadata["language"] = value
            elif tag == "meta":
                key = element.get("name")
                if key and value:
                    doc.metadata[f"meta_{key}"] = value

    def _read_styles(
        self, package: zipfile.ZipFile, names: list[str], doc: ParsedDocument
    ) -> dict[str, tuple[str, str]]:
        """Map styleIDRef -> (name, engName) from Contents/header.xml."""
        if "Contents/header.xml" not in names:
            doc.add_warning("HEADER_PART_MISSING")
            return {}
        try:
            root = self._load_xml(package, "Contents/header.xml")
        except Exception:
            doc.add_warning("HEADER_PART_UNREADABLE")
            return {}
        styles: dict[str, tuple[str, str]] = {}
        for element in root.iter(f"{{{NS_HEAD}}}style"):
            style_id = element.get("id")
            if style_id is not None:
                styles[style_id] = (element.get("name") or "", element.get("engName") or "")
        return styles

    # -- body walk ---------------------------------------------------------

    def _walk(self, element: ET.Element, ctx: _Context, doc: ParsedDocument) -> None:
        """Walk the section tree in document order."""
        for child in element:
            tag = _local(child.tag)
            if tag == "p":
                self._emit_paragraph(child, ctx, doc)
            elif tag == "tbl":
                self._emit_table(child, ctx, doc)
            else:
                self._walk(child, ctx, doc)

    def _emit_paragraph(self, element: ET.Element, ctx: _Context, doc: ParsedDocument) -> None:
        texts: list[str] = []
        containers: list[ET.Element] = []

        for run in element:
            if _local(run.tag) != "run":
                continue
            for node in run:
                if _local(node.tag) == "t":
                    texts.append(_text_of_t(node))
                else:
                    # Tables, drawing objects, controls: visited after this
                    # paragraph's own text so reading order is preserved.
                    containers.append(node)

        text = "".join(texts)

        if ctx.cell_sink is not None:
            ctx.cell_sink.append(text)
        else:
            style_id = element.get("styleIDRef")
            style_name, eng_name = ctx.styles.get(style_id, ("", "")) if style_id else ("", "")
            is_heading = (
                not ctx.in_shape
                and bool(style_name or eng_name)
                and _is_heading_style(style_name, eng_name)
            )
            para = ParsedParagraph(
                index=len(doc.paragraphs),
                text=text,
                page_number=None,
                section_title=ctx.section_title,
                paragraph_type=(
                    "heading" if is_heading else ("textbox" if ctx.in_shape else "body")
                ),
                section_index=ctx.section_index,
                style_name=style_name or None,
            )
            if is_heading:
                stripped = text.strip()
                if stripped:
                    ctx.section_title = stripped
                    para.section_title = stripped
            doc.paragraphs.append(para)

        for container in containers:
            if _local(container.tag) == "tbl":
                self._emit_table(container, ctx, doc)
            else:
                # Text inside a drawing object is body content in Korean
                # business documents; keep it, but mark where it came from.
                shape_ctx = _Context(
                    section_index=ctx.section_index,
                    cell_sink=ctx.cell_sink,
                    in_shape=True,
                    section_title=ctx.section_title,
                    styles=ctx.styles,
                )
                self._walk(container, shape_ctx, doc)

    def _emit_table(self, element: ET.Element, ctx: _Context, doc: ParsedDocument) -> None:
        if ctx.cell_sink is not None:
            doc.add_warning("NESTED_TABLE_FLATTENED")

        declared_rows = _int_or_none(element.get("rowCnt"))
        declared_cols = _int_or_none(element.get("colCnt"))
        anchor = len(doc.paragraphs) - 1 if doc.paragraphs else None

        cells: list[ParsedCell] = []
        address_missing = False

        for row_position, row_element in enumerate(
            [e for e in element if _local(e.tag) == "tr"]
        ):
            column_cursor = 0
            for column_position, cell_element in enumerate(
                [e for e in row_element if _local(e.tag) == "tc"]
            ):
                addr = cell_element.find(f"{{{NS_PARA}}}cellAddr")
                span = cell_element.find(f"{{{NS_PARA}}}cellSpan")
                column = _int_or_none(addr.get("colAddr")) if addr is not None else None
                row = _int_or_none(addr.get("rowAddr")) if addr is not None else None
                if column is None or row is None:
                    # Minimal / hand-written HWPX omits <hp:cellAddr>. Fall back
                    # to the position in the <hp:tr>/<hp:tc> order rather than
                    # dropping the cell's content.
                    address_missing = True
                    row = row_position
                    column = column_cursor if column_cursor else column_position

                sink: list[str] = []
                cell_ctx = _Context(
                    section_index=ctx.section_index,
                    cell_sink=sink,
                    in_shape=ctx.in_shape,
                    section_title=ctx.section_title,
                    styles=ctx.styles,
                )
                self._walk(cell_element, cell_ctx, doc)

                column_span = (
                    _int_or_none(span.get("colSpan")) if span is not None else None
                ) or 1
                row_span = (
                    _int_or_none(span.get("rowSpan")) if span is not None else None
                ) or 1
                cells.append(
                    ParsedCell(
                        row=row,
                        column=column,
                        text="\n".join(t for t in sink if t).strip(),
                        row_span=row_span,
                        column_span=column_span,
                    )
                )
                column_cursor = column + column_span

        if address_missing:
            doc.add_warning("TABLE_CELL_ADDRESS_UNREADABLE")

        has_merged = any(c.is_merged for c in cells)
        if has_merged:
            doc.add_warning("MERGED_CELLS_PRESENT")

        if cells:
            row_total = max(c.row + c.row_span for c in cells)
            col_total = max(c.column + c.column_span for c in cells)
            rows = [["" for _ in range(col_total)] for _ in range(row_total)]
            for cell in cells:
                if rows[cell.row][cell.column] and cell.text:
                    # Two cells claim the same slot -- keep both rather than
                    # losing one, and make the collision visible.
                    doc.add_warning("TABLE_CELL_ADDRESS_COLLISION")
                    rows[cell.row][cell.column] += "\n" + cell.text
                else:
                    rows[cell.row][cell.column] = cell.text
        else:
            rows = []

        table = ParsedTable(
            index=len(doc.tables),
            rows=rows,
            cells=cells,
            page_number=None,
            paragraph_index=anchor,
            section_index=ctx.section_index,
            declared_row_count=declared_rows,
            declared_column_count=declared_cols,
            has_merged_cells=has_merged,
        )

        structure_lost = False
        if declared_rows is not None and table.row_count != declared_rows:
            doc.add_warning("TABLE_ROW_COUNT_MISMATCH")
            structure_lost = True
        if declared_cols is not None and table.column_count != declared_cols:
            doc.add_warning("TABLE_COLUMN_COUNT_MISMATCH")
            structure_lost = True
        if has_merged and (structure_lost or address_missing):
            doc.add_warning("MERGED_CELL_STRUCTURE_LOSS")

        doc.tables.append(table)


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
