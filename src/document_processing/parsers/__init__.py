"""Parser registry.

Callers should go through :func:`get_parser` / :func:`parse_document` rather
than importing a concrete parser, so that adding a format is a registry change
and nothing else -- which is all DOCX support was above this line.
"""

from __future__ import annotations

from pathlib import Path

from ..models import ParsedDocument
from .base import BaseParser, DocumentParser
from .exceptions import (
    ERROR_CODES,
    CorruptDocumentError,
    DocumentParseError,
    EmptyDocumentError,
    EncryptedDocumentError,
    OCRRequiredError,
    ParseFailedError,
    UnsupportedFormatError,
)
from .docx import DocxParser
from .hwp import HwpParser
from .hwpx import HwpxParser
from .pdf import PdfParser

#: Registration order decides which parser wins when several claim a file.
#: Each claims a distinct extension, so order is not load-bearing today.
_PARSERS: tuple[DocumentParser, ...] = (HwpxParser(), HwpParser(), DocxParser(), PdfParser())


def available_parsers() -> tuple[DocumentParser, ...]:
    return _PARSERS


def get_parser(file_path: Path | str) -> DocumentParser:
    """Return the parser that claims ``file_path``.

    Raises :class:`UnsupportedFormatError` if none does -- the caller should
    record that as a per-file failure, not treat it as a crash.
    """
    path = Path(file_path)
    for parser in _PARSERS:
        if parser.supports(path):
            return parser
    raise UnsupportedFormatError(f"no parser registered for '{path.suffix}'")


def parse_document(file_path: Path | str) -> ParsedDocument:
    """Select a parser and parse. Every failure is a :class:`DocumentParseError`."""
    path = Path(file_path)
    if not path.exists():
        raise ParseFailedError(f"file not found: {path}", detail="FILE_NOT_FOUND")
    return get_parser(path).parse(path)


__all__ = [
    "ERROR_CODES",
    "BaseParser",
    "DocxParser",
    "PdfParser",
    "CorruptDocumentError",
    "DocumentParseError",
    "DocumentParser",
    "EmptyDocumentError",
    "EncryptedDocumentError",
    "HwpParser",
    "HwpxParser",
    "OCRRequiredError",
    "ParseFailedError",
    "UnsupportedFormatError",
    "available_parsers",
    "get_parser",
    "parse_document",
]
