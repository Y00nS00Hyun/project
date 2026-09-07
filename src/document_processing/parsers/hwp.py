"""HWP 5.x parser.

Container handling uses ``olefile`` (BSD-2-Clause); the record layer is our own
(:mod:`._hwp_records`), so the only third-party surface is a well-established
read-only CFB reader.  See docs/poc/hwp-poc-report.md sections 3 and 11 for why
the alternatives were rejected.

Deliberate non-goals:

* Hancom "distribution" (배포용) documents are reported as ENCRYPTED.  Their body
  lives in an encrypted ``ViewText`` stream; this PoC does not attempt to
  decrypt content protection.
* Page numbers are not produced.  HWP stores a line-layout cache, not page
  numbers, and the corpus measurement in the report shows the page-first-line
  flag is not populated in practice.  Anything else would be a guess.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from ..models import ParsedCell, ParsedDocument, ParsedParagraph, ParsedTable
from . import _hwp_records as rec
from .base import BaseParser
from .exceptions import CorruptDocumentError, EncryptedDocumentError, UnsupportedFormatError

PARSER_NAME = "inhouse-hwp5"
PARSER_VERSION = "0.1.0"

HWP_SIGNATURE = b"HWP Document File"

#: Style names (Hangul and English) that the document itself uses for outline
#: headings.  Matching is prefix-based and nothing else is treated as a heading.
_HEADING_STYLE_PREFIXES = ("개요", "제목", "Outline", "Heading", "Title")


def _is_heading_style(name: str, eng_name: str) -> bool:
    for candidate in (name, eng_name):
        c = candidate.strip()
        if c and any(c.startswith(p) for p in _HEADING_STYLE_PREFIXES):
            return True
    return False


class HwpParser(BaseParser):
    """Parses HWP 5.x binary documents into a :class:`ParsedDocument`."""

    name = PARSER_NAME
    version = PARSER_VERSION
    extensions = (".hwp",)

    # -- entry point -------------------------------------------------------

    def _parse(self, file_path: Path) -> ParsedDocument:
        try:
            import olefile
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise UnsupportedFormatError(
                "olefile is required to parse HWP 5.x documents"
            ) from exc

        raw_head = file_path.read_bytes()[:8]
        if raw_head[:4] == b"PK\x03\x04":
            # Very common in the wild: an HWPX saved with a .hwp extension.
            raise UnsupportedFormatError(
                "file is a ZIP container, not an OLE compound file; "
                "it is most likely HWPX with a .hwp extension"
            )
        if not olefile.isOleFile(str(file_path)):
            raise CorruptDocumentError("not an OLE compound file")

        ole = olefile.OleFileIO(str(file_path))
        try:
            return self._parse_ole(ole, file_path)
        finally:
            ole.close()

    # -- container ---------------------------------------------------------

    def _parse_ole(self, ole, file_path: Path) -> ParsedDocument:
        streams = {"/".join(entry) for entry in ole.listdir()}
        if "FileHeader" not in streams:
            raise CorruptDocumentError("FileHeader stream is missing")

        header = ole.openstream("FileHeader").read()
        if len(header) < 40:
            raise CorruptDocumentError(f"FileHeader is {len(header)} bytes, expected >= 40")
        if not header.startswith(HWP_SIGNATURE):
            raise UnsupportedFormatError("FileHeader signature is not 'HWP Document File'")

        version = f"{header[35]}.{header[34]}.{header[33]}.{header[32]}"
        (flags,) = struct.unpack_from("<I", header, 36)

        if flags & rec.FLAG_PASSWORD:
            raise EncryptedDocumentError(
                "document is password protected", detail="PASSWORD_PROTECTED"
            )
        if flags & rec.FLAG_DRM:
            raise EncryptedDocumentError("document is DRM protected", detail="DRM")
        if flags & rec.FLAG_DISTRIBUTION:
            # Body text lives in an encrypted ViewText/SectionN stream.
            raise EncryptedDocumentError(
                "distribution (배포용) document; body text is stored encrypted "
                "in ViewText streams",
                detail="DISTRIBUTION_DOCUMENT",
            )

        compressed = bool(flags & rec.FLAG_COMPRESSED)

        doc = ParsedDocument(
            file_path=str(file_path),
            file_type="hwp",
            parser_name=self.name,
            parser_version=self.version,
        )
        doc.metadata.update(
            {
                "hwp_version": version,
                "file_header_flags": flags,
                "compressed": compressed,
                "image_count": sum(1 for s in streams if s.startswith("BinData/")),
            }
        )
        # HWP does not record a page count anywhere we can trust.
        doc.page_count = None

        styles = self._read_styles(ole, compressed, doc)

        section_names = sorted(
            (s for s in streams if s.startswith("BodyText/Section")),
            key=_section_sort_key,
        )
        if not section_names:
            raise CorruptDocumentError("no BodyText/Section streams found")
        doc.metadata["section_count"] = len(section_names)

        for section_index, stream_name in enumerate(section_names):
            body = self._read_section(ole, stream_name, compressed)
            self._parse_section(body, section_index, styles, doc)

        doc.add_warning("PAGE_NUMBER_NOT_AVAILABLE")
        if not doc.has_section_titles():
            doc.add_warning("SECTION_TITLE_NOT_AVAILABLE")
        return doc

    @staticmethod
    def _read_section(ole, stream_name: str, compressed: bool) -> bytes:
        raw = ole.openstream(stream_name).read()
        if not compressed:
            return raw
        try:
            # Raw deflate, no zlib wrapper.
            return zlib.decompress(raw, -15)
        except zlib.error as exc:
            raise CorruptDocumentError(
                f"{stream_name} could not be decompressed: {exc}"
            ) from exc

    def _read_styles(self, ole, compressed: bool, doc: ParsedDocument) -> dict[int, tuple[str, str]]:
        """Map style id -> (name, engName) from the DocInfo stream.

        Failure here is non-fatal: heading detection degrades, body text does
        not.  The degradation is recorded as a warning.
        """
        try:
            info = self._read_section(ole, "DocInfo", compressed)
        except Exception:
            doc.add_warning("DOCINFO_UNREADABLE")
            return {}
        styles: dict[int, tuple[str, str]] = {}
        try:
            style_id = 0
            for record in rec.iter_records(info):
                if record.tag_id == rec.HWPTAG_STYLE:
                    styles[style_id] = rec.decode_style_names(record.payload)
                    style_id += 1
        except rec.RecordStreamError:
            doc.add_warning("DOCINFO_TRUNCATED")
        return styles

    # -- body --------------------------------------------------------------

    def _parse_section(
        self,
        body: bytes,
        section_index: int,
        styles: dict[int, tuple[str, str]],
        doc: ParsedDocument,
    ) -> None:
        try:
            records = list(rec.iter_records(body))
        except rec.RecordStreamError as exc:
            raise CorruptDocumentError(f"section {section_index}: {exc}") from exc

        current_section_title: str | None = None
        pending_style_id: int | None = None
        # (record level, control id) of the controls we are currently inside.
        # Korean business documents routinely lay body text out inside drawing
        # objects, so those paragraphs must be collected too -- only table cells
        # are handled elsewhere.
        containers: list[tuple[int, str]] = []
        index = 0

        while index < len(records):
            record = records[index]

            while containers and record.level <= containers[-1][0]:
                containers.pop()

            if record.tag_id == rec.HWPTAG_CTRL_HEADER:
                ctrl_id = rec.decode_ctrl_id(record.payload)
                if ctrl_id == rec.CTRL_ID_TABLE:
                    index = self._consume_table(records, index, section_index, doc)
                    continue
                containers.append((record.level, ctrl_id))
                index += 1
                continue

            if record.tag_id == rec.HWPTAG_PARA_HEADER:
                head = rec.decode_para_header(record.payload)
                pending_style_id = head.style_id if head else None
                index += 1
                continue

            if record.tag_id == rec.HWPTAG_PARA_TEXT:
                style_name, eng_name = (
                    styles.get(pending_style_id, ("", ""))
                    if pending_style_id is not None
                    else ("", "")
                )
                text = rec.decode_para_text(record.payload)
                in_shape = any(cid == rec.CTRL_ID_GSO for _, cid in containers)
                is_heading = (
                    not in_shape
                    and bool(style_name or eng_name)
                    and _is_heading_style(style_name, eng_name)
                )
                para = ParsedParagraph(
                    index=len(doc.paragraphs),
                    text=text,
                    page_number=None,
                    section_title=current_section_title,
                    paragraph_type=(
                        "heading" if is_heading else ("textbox" if in_shape else "body")
                    ),
                    section_index=section_index,
                    style_name=style_name or None,
                )
                if is_heading:
                    stripped = text.strip()
                    if stripped:
                        current_section_title = stripped
                        # A heading titles what follows it, not itself.
                        para.section_title = stripped
                doc.paragraphs.append(para)
                index += 1
                continue

            index += 1

    def _consume_table(
        self,
        records: list[rec.Record],
        start: int,
        section_index: int,
        doc: ParsedDocument,
    ) -> int:
        """Read one table starting at the CTRL_HEADER at ``start``.

        Returns the index of the first record after the table.
        """
        ctrl_level = records[start].level
        index = start + 1

        props: rec.TableProperties | None = None
        cells: list[ParsedCell] = []
        current: rec.CellProperties | None = None
        current_texts: list[str] = []
        unreadable_cells = 0

        def flush() -> None:
            nonlocal current, current_texts
            if current is None:
                return
            cells.append(
                ParsedCell(
                    row=current.row,
                    column=current.column,
                    text="\n".join(t for t in current_texts if t).strip(),
                    row_span=current.row_span,
                    column_span=current.column_span,
                )
            )
            current = None
            current_texts = []

        while index < len(records):
            record = records[index]
            # A record back at (or above) the control's own level ends the table.
            if record.level <= ctrl_level:
                break

            if record.tag_id == rec.HWPTAG_TABLE and props is None:
                try:
                    props = rec.decode_table(record.payload)
                except rec.RecordStreamError:
                    doc.add_warning("TABLE_PROPERTIES_UNREADABLE")
            elif record.tag_id == rec.HWPTAG_LIST_HEADER:
                flush()
                current = rec.decode_cell(record.payload)
                if current is None:
                    unreadable_cells += 1
            elif record.tag_id == rec.HWPTAG_PARA_TEXT and current is not None:
                current_texts.append(rec.decode_para_text(record.payload))
            elif record.tag_id == rec.HWPTAG_CTRL_HEADER:
                # A nested table inside a cell: recurse so its content is not
                # attributed to the outer cell.
                if rec.decode_ctrl_id(record.payload) == rec.CTRL_ID_TABLE:
                    flush()
                    doc.add_warning("NESTED_TABLE_FLATTENED")
                    index = self._consume_table(records, index, section_index, doc)
                    continue
            index += 1

        flush()

        if unreadable_cells:
            doc.add_warning("TABLE_CELL_ADDRESS_UNREADABLE")

        anchor = len(doc.paragraphs) - 1 if doc.paragraphs else None
        table = _build_table(
            table_index=len(doc.tables),
            cells=cells,
            props=props,
            paragraph_index=anchor,
            section_index=section_index,
            doc=doc,
        )
        doc.tables.append(table)
        return index


def _section_sort_key(name: str) -> tuple[int, str]:
    """Sort BodyText/Section10 after Section9, not after Section1."""
    tail = name.rsplit("Section", 1)[-1]
    return (int(tail), name) if tail.isdigit() else (1 << 30, name)


def _build_table(
    *,
    table_index: int,
    cells: list[ParsedCell],
    props: rec.TableProperties | None,
    paragraph_index: int | None,
    section_index: int,
    doc: ParsedDocument,
) -> ParsedTable:
    """Lay decoded cells out on a grid using the addresses the file declares."""
    has_merged = any(c.is_merged for c in cells)
    if has_merged:
        # Informational only. Spans are preserved on ParsedCell and the anchor
        # cell keeps its text, so the grid is still reconstructed. Actual loss
        # is reported separately below.
        doc.add_warning("MERGED_CELLS_PRESENT")

    if cells:
        row_total = max(c.row + c.row_span for c in cells)
        col_total = max(c.column + c.column_span for c in cells)
        rows: list[list[str]] = [["" for _ in range(col_total)] for _ in range(row_total)]
        for cell in cells:
            if cell.row < row_total and cell.column < col_total:
                rows[cell.row][cell.column] = cell.text
    else:
        rows = []

    table = ParsedTable(
        index=table_index,
        rows=rows,
        cells=cells,
        page_number=None,
        paragraph_index=paragraph_index,
        section_index=section_index,
        declared_row_count=props.row_count if props else None,
        declared_column_count=props.column_count if props else None,
        has_merged_cells=has_merged,
    )

    structure_lost = False
    if props is not None:
        expected = sum(props.row_sizes) if props.row_sizes else None
        if expected is not None and expected != len(cells):
            doc.add_warning("TABLE_CELL_COUNT_MISMATCH")
            structure_lost = True
        if props.row_count and table.row_count != props.row_count:
            doc.add_warning("TABLE_ROW_COUNT_MISMATCH")
            structure_lost = True
        if props.column_count and table.column_count != props.column_count:
            doc.add_warning("TABLE_COLUMN_COUNT_MISMATCH")
            structure_lost = True

    # Only claim structure loss when the recovered grid actually disagrees with
    # what the document declares -- merges alone are handled correctly.
    if has_merged and structure_lost:
        doc.add_warning("MERGED_CELL_STRUCTURE_LOSS")
    return table
