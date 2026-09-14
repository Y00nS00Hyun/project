"""Build small PDFs for tests, with pypdf's own writer objects.

The host has no PDF authoring tool (no LibreOffice, Ghostscript, reportlab),
and committing binary fixtures would hide what each one contains. So pages are
assembled here from content streams: text is drawn with a Type0 font whose
ToUnicode CMap maps each 2-byte code to the same UTF-16 code unit, which is
enough for a text layer that extraction tools read -- including Hangul. No
glyphs are embedded, so a viewer may render boxes; extraction is what the
tests care about.

Everything written is synthetic. No real document ever passes through here.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

PAGE_WIDTH = 595
PAGE_HEIGHT = 842


def _tounicode_cmap() -> bytes:
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    # A bfrange may only vary in its last byte, so one per high byte, at most
    # 100 per block. Surrogates are skipped: fixtures never contain them.
    ranges = [f"<{hi:02X}00> <{hi:02X}FF> <{hi:02X}00>" for hi in range(256)
              if not 0xD8 <= hi <= 0xDF]
    for start in range(0, len(ranges), 100):
        block = ranges[start:start + 100]
        lines.append(f"{len(block)} beginbfrange")
        lines.extend(block)
        lines.append("endbfrange")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("ascii")


def _font(writer: PdfWriter):
    cmap = DecodedStreamObject()
    cmap.set_data(_tounicode_cmap())
    descriptor = DictionaryObject({
        NameObject("/Type"): NameObject("/FontDescriptor"),
        NameObject("/FontName"): NameObject("/HYGoThic-Medium"),
        NameObject("/Flags"): NumberObject(4),
        NameObject("/FontBBox"): ArrayObject([NumberObject(v) for v in (0, -120, 1000, 880)]),
        NameObject("/ItalicAngle"): NumberObject(0),
        NameObject("/Ascent"): NumberObject(880),
        NameObject("/Descent"): NumberObject(-120),
        NameObject("/CapHeight"): NumberObject(700),
        NameObject("/StemV"): NumberObject(80),
    })
    cid_font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/CIDFontType0"),
        NameObject("/BaseFont"): NameObject("/HYGoThic-Medium"),
        NameObject("/CIDSystemInfo"): DictionaryObject({
            NameObject("/Registry"): TextStringObject("Adobe"),
            NameObject("/Ordering"): TextStringObject("Identity"),
            NameObject("/Supplement"): NumberObject(0),
        }),
        NameObject("/FontDescriptor"): writer._add_object(descriptor),
        NameObject("/DW"): NumberObject(1000),
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/HYGoThic-Medium"),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([writer._add_object(cid_font)]),
        NameObject("/ToUnicode"): writer._add_object(cmap),
    })
    return writer._add_object(font)


def _hex(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()


def _set_page(writer: PdfWriter, content: bytes, resources: DictionaryObject) -> None:
    page = writer.add_blank_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = resources


def text_page_content(paragraphs: list[list[str]]) -> bytes:
    """Draw paragraphs of lines: 16pt between lines, 40pt between paragraphs."""
    ops = ["BT", "/F1 12 Tf", f"72 {PAGE_HEIGHT - 72} Td"]
    first = True
    for paragraph in paragraphs:
        for line in paragraph:
            if not first:
                ops.append("0 -16 Td")
            first = False
            ops.append(f"<{_hex(line)}> Tj")
        ops.append("0 -24 Td")
    ops.append("ET")
    return "\n".join(ops).encode("ascii")


def write_text_pdf(path: Path, pages: list[list[list[str]]], *,
                   user_password: str | None = None,
                   owner_password: str | None = None) -> Path:
    """One entry per page; each page is paragraphs; each paragraph is lines."""
    writer = PdfWriter()
    font = _font(writer)
    for paragraphs in pages:
        resources = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        _set_page(writer, text_page_content(paragraphs), resources)
    if user_password is not None or owner_password is not None:
        # RC4 needs no crypto library, which is all this environment has.
        writer.encrypt(user_password=user_password or "",
                       owner_password=owner_password, algorithm="RC4-128")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path


def write_image_only_pdf(path: Path, pages: int = 1) -> Path:
    """Pages that draw one image and no text -- what a scan looks like."""
    writer = PdfWriter()
    for _ in range(pages):
        image = DecodedStreamObject()
        image.set_data(b"\x80")
        image.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        })
        resources = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({
                NameObject("/Im0"): writer._add_object(image),
            }),
        })
        _set_page(writer, b"q 400 0 0 600 90 120 cm /Im0 Do Q", resources)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        writer.write(handle)
    return path
