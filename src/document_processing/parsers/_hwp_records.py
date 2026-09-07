"""Low-level HWP 5.x record decoding.

Split out from :mod:`document_processing.parsers.hwp` on purpose: everything
here operates on plain ``bytes`` and can be unit-tested without an OLE
container or a real .hwp file.

Structures follow the publicly published "한글 문서 파일 형식 5.0" specification.
Every offset used below was additionally verified against a real-world HWP
corpus during the PoC (see docs/poc/hwp-poc-report.md section 5).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

# -- record tag ids (HWPTAG_BEGIN = 0x010) ---------------------------------
HWPTAG_BEGIN = 0x010

# DocInfo stream
HWPTAG_STYLE = HWPTAG_BEGIN + 10         # 26

# BodyText/Section streams
HWPTAG_PARA_HEADER = HWPTAG_BEGIN + 50   # 66
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51     # 67
HWPTAG_CTRL_HEADER = HWPTAG_BEGIN + 55   # 71
HWPTAG_LIST_HEADER = HWPTAG_BEGIN + 56   # 72
HWPTAG_TABLE = HWPTAG_BEGIN + 61         # 77

# -- FileHeader flag bits ---------------------------------------------------
FLAG_COMPRESSED = 0x01
FLAG_PASSWORD = 0x02
FLAG_DISTRIBUTION = 0x04
FLAG_DRM = 0x10

# -- PARA_TEXT control character classes ------------------------------------
# A "char" control occupies 1 WCHAR; "inline" and "extended" controls occupy 8
# WCHARs (the control code is repeated as the 8th).
CHAR_CONTROLS = frozenset({0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31})
INLINE_CONTROLS = frozenset({4, 5, 6, 7, 8, 9, 19, 20})
EXTENDED_CONTROLS = frozenset({1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23})

CONTROL_SPAN_WCHARS = 8

#: Control ids (already byte-reversed to reading order) we care about.
CTRL_ID_TABLE = "tbl "
CTRL_ID_GSO = "gso "


class RecordStreamError(ValueError):
    """The record stream is truncated or self-inconsistent."""


@dataclass(frozen=True)
class Record:
    tag_id: int
    level: int
    payload: bytes


def iter_records(buf: bytes) -> Iterator[Record]:
    """Walk a decompressed HWP record stream.

    Each record starts with a 32-bit header packing ``tag_id`` (10 bits),
    ``level`` (10 bits) and ``size`` (12 bits); a size of 0xFFF means the real
    size follows as a separate 32-bit value.

    Raises :class:`RecordStreamError` on truncation so the caller can report
    CORRUPT rather than silently returning a short document.
    """
    pos = 0
    total = len(buf)
    while pos < total:
        if pos + 4 > total:
            raise RecordStreamError(
                f"truncated record header at offset {pos} of {total}"
            )
        (header,) = struct.unpack_from("<I", buf, pos)
        pos += 4
        tag_id = header & 0x3FF
        level = (header >> 10) & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > total:
                raise RecordStreamError(f"truncated extended size at offset {pos}")
            (size,) = struct.unpack_from("<I", buf, pos)
            pos += 4
        if pos + size > total:
            raise RecordStreamError(
                f"record tag={tag_id} claims {size} bytes but only "
                f"{total - pos} remain at offset {pos}"
            )
        yield Record(tag_id, level, buf[pos : pos + size])
        pos += size


def decode_para_text(payload: bytes, *, object_placeholder: str = "") -> str:
    """Decode a HWPTAG_PARA_TEXT payload into plain text.

    The payload is UTF-16LE with embedded control codes.  Text characters are
    kept verbatim (so Korean, digits and punctuation pass through untouched);
    tabs and line breaks become their ASCII equivalents; control objects are
    skipped, optionally leaving ``object_placeholder`` behind.
    """
    out: list[str] = []
    pos = 0
    limit = len(payload) - 1  # need 2 bytes for a WCHAR
    while pos < limit:
        (code,) = struct.unpack_from("<H", payload, pos)
        if code in CHAR_CONTROLS:
            if code == 9:
                out.append("\t")
            elif code in (10, 13):
                out.append("\n")
            elif code in (24, 30, 31):
                # 24 hyphen, 30 no-break space, 31 fixed-width space
                out.append("-" if code == 24 else " ")
            pos += 2
        elif code in INLINE_CONTROLS or code in EXTENDED_CONTROLS:
            if code in EXTENDED_CONTROLS and object_placeholder:
                out.append(object_placeholder)
            pos += 2 * CONTROL_SPAN_WCHARS
        else:
            out.append(chr(code))
            pos += 2
    return "".join(out)


def decode_ctrl_id(payload: bytes) -> str:
    """Read the 4-character control id from a HWPTAG_CTRL_HEADER payload.

    The id is stored little-endian, so ``b' lbt'`` reads as ``"tbl "``.
    """
    if len(payload) < 4:
        return ""
    return payload[:4][::-1].decode("ascii", errors="replace")


@dataclass(frozen=True)
class TableProperties:
    row_count: int
    column_count: int
    #: Number of cells in each row, as declared by the file.  This is what makes
    #: row segmentation reliable even when cells are merged.
    row_sizes: tuple[int, ...]


def decode_table(payload: bytes) -> TableProperties:
    """Decode a HWPTAG_TABLE payload.

    Layout: UINT32 property, UINT16 nRows, UINT16 nCols, UINT16 cellSpacing,
    4 x INT16 inner margins, then ``nRows`` x UINT16 giving the cell count of
    each row.
    """
    if len(payload) < 18:
        raise RecordStreamError(f"table record too short: {len(payload)} bytes")
    row_count, column_count = struct.unpack_from("<HH", payload, 4)
    # 4 property + 2 nRows + 2 nCols + 2 cellSpacing + 4 x INT16 inner margins
    offset = 18
    row_sizes: list[int] = []
    for _ in range(row_count):
        if offset + 2 > len(payload):
            # Declaration and payload disagree; the caller downgrades to
            # sequential row grouping and records a warning.
            break
        (size,) = struct.unpack_from("<H", payload, offset)
        row_sizes.append(size)
        offset += 2
    return TableProperties(row_count, column_count, tuple(row_sizes))


@dataclass(frozen=True)
class CellProperties:
    column: int
    row: int
    column_span: int
    row_span: int
    paragraph_count: int


#: Offset of the cell-address block inside a table cell's HWPTAG_LIST_HEADER
#: payload (UINT32 paragraph count + UINT32 property precede it).
CELL_ATTR_OFFSET = 8

#: Cell addresses beyond this are treated as unreadable rather than trusted.
MAX_CELL_ADDRESS = 4096


def decode_cell(payload: bytes) -> CellProperties | None:
    """Decode the cell attributes of a table-cell HWPTAG_LIST_HEADER.

    Returns ``None`` when the payload is too short or the decoded addresses are
    implausible -- the caller then falls back to sequential placement and warns,
    instead of trusting a bad grid.
    """
    if len(payload) < CELL_ATTR_OFFSET + 8:
        return None
    (paragraph_count,) = struct.unpack_from("<I", payload, 0)
    column, row, column_span, row_span = struct.unpack_from(
        "<HHHH", payload, CELL_ATTR_OFFSET
    )
    if column > MAX_CELL_ADDRESS or row > MAX_CELL_ADDRESS:
        return None
    if not (1 <= column_span <= MAX_CELL_ADDRESS) or not (1 <= row_span <= MAX_CELL_ADDRESS):
        return None
    return CellProperties(column, row, column_span, row_span, paragraph_count)


def decode_style_names(payload: bytes) -> tuple[str, str]:
    """Decode the local and English style name from a HWPTAG_STYLE payload.

    Layout starts with two WCHAR-counted strings: the Hangul name then the
    English name.  Returns ``("", "")`` if the payload does not fit that shape.
    """
    try:
        pos = 0
        names: list[str] = []
        for _ in range(2):
            (length,) = struct.unpack_from("<H", payload, pos)
            pos += 2
            raw = payload[pos : pos + length * 2]
            if len(raw) != length * 2:
                return ("", "")
            names.append(raw.decode("utf-16-le", errors="replace"))
            pos += length * 2
        return (names[0], names[1])
    except struct.error:
        return ("", "")


@dataclass(frozen=True)
class ParaHeader:
    char_count: int
    para_shape_id: int
    style_id: int


def decode_para_header(payload: bytes) -> ParaHeader | None:
    """Decode HWPTAG_PARA_HEADER: UINT32 nChars, UINT32 ctrlMask,
    UINT16 paraShapeId, UINT8 styleId, ..."""
    if len(payload) < 11:
        return None
    char_count, _ctrl_mask, para_shape_id = struct.unpack_from("<IIH", payload, 0)
    style_id = payload[10]
    # The top bit of nChars flags the count unit; mask it off.
    return ParaHeader(char_count & 0x7FFFFFFF, para_shape_id, style_id)
