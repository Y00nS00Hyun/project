"""Document processing for the internal document management system.

Scope of this package today is the HWP/HWPX parsing PoC only: turn a file into
a structured :class:`~document_processing.models.ParsedDocument` plus a
normalized searchable rendering.  Chunking, embedding, storage and search are
explicitly out of scope until the PoC conclusion is accepted -- see
``docs/poc/hwp-poc-report.md``.
"""

from __future__ import annotations

from .models import ParsedCell, ParsedDocument, ParsedParagraph, ParsedTable
from .normalize import (
    NORMALIZER_VERSION,
    build_normalized_text,
    canonical_json,
    canonicalize,
    content_hash,
    normalize_text,
    normalized_text_hash,
)
from .parsers import (
    ERROR_CODES,
    DocumentParseError,
    DocumentParser,
    available_parsers,
    get_parser,
    parse_document,
)

__version__ = "0.1.0"

__all__ = [
    "ERROR_CODES",
    "NORMALIZER_VERSION",
    "DocumentParseError",
    "DocumentParser",
    "ParsedCell",
    "ParsedDocument",
    "ParsedParagraph",
    "ParsedTable",
    "__version__",
    "available_parsers",
    "build_normalized_text",
    "canonical_json",
    "canonicalize",
    "content_hash",
    "get_parser",
    "normalize_text",
    "normalized_text_hash",
    "parse_document",
]
