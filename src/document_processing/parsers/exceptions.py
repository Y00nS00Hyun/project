"""Error taxonomy for document parsing.

Every failure a parser reports is a :class:`DocumentParseError` carrying a
stable ``error_code``.  Ingestion is expected to persist the code, not the
message, so failures can be counted and re-driven per class.

Design rule enforced throughout the parsers: **never guess a specific cause**.
If the underlying format/library does not let us distinguish the reason, the
failure is reported as ``PARSE_FAILED`` rather than as a more specific code that
might be wrong.
"""

from __future__ import annotations


class DocumentParseError(Exception):
    """Base class for every parse failure.

    A worker only needs to catch this type (plus a defensive ``Exception``
    guard) to keep one bad file from taking down a batch.
    """

    error_code: str = "PARSE_FAILED"

    def __init__(self, message: str = "", *, detail: str | None = None) -> None:
        super().__init__(message or self.error_code)
        self.message = message or self.error_code
        # Optional sub-classification, e.g. DISTRIBUTION_DOCUMENT for an
        # ENCRYPTED HWP.  Kept separate so the primary code stays coarse.
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"[{self.error_code}/{self.detail}] {self.message}"
        return f"[{self.error_code}] {self.message}"


class UnsupportedFormatError(DocumentParseError):
    """The file is not a format this parser handles."""

    error_code = "UNSUPPORTED_FORMAT"


class EncryptedDocumentError(DocumentParseError):
    """The document's content is encrypted and cannot be read.

    Covers both password-protected HWP files and Hancom "distribution"
    (배포용) documents, whose body lives in an encrypted ``ViewText`` stream.
    """

    error_code = "ENCRYPTED"


class CorruptDocumentError(DocumentParseError):
    """The container or its internal structure is damaged."""

    error_code = "CORRUPT"


class OCRRequiredError(DocumentParseError):
    """The document opened fine but carries no extractable text, and the file
    itself shows it is image-based.

    Only raised when both conditions hold; an ambiguous case is reported as a
    warning on a successful parse instead.
    """

    error_code = "OCR_REQUIRED"


class EmptyDocumentError(DocumentParseError):
    """The document opened fine and genuinely contains no content."""

    error_code = "EMPTY_DOCUMENT"


class ParseFailedError(DocumentParseError):
    """Catch-all for failures we cannot attribute to a specific cause."""

    error_code = "PARSE_FAILED"


#: All error codes this package can produce, in the order they are reported in
#: the PoC report.  Ingestion should treat this as the closed set.
ERROR_CODES: tuple[str, ...] = (
    "UNSUPPORTED_FORMAT",
    "ENCRYPTED",
    "CORRUPT",
    "OCR_REQUIRED",
    "PARSE_FAILED",
    "EMPTY_DOCUMENT",
)
