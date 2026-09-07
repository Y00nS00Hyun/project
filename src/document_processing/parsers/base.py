"""Parser abstraction.

The rest of the system talks to :class:`DocumentParser` only.  Swapping the
library behind ``HwpParser`` or ``HwpxParser`` -- or adding a DOCX/PDF parser
later -- must not require changes above this boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import ParsedDocument
from .exceptions import (
    DocumentParseError,
    EmptyDocumentError,
    OCRRequiredError,
    ParseFailedError,
)

#: A document is only treated as carrying *no* text when it has zero
#: non-whitespace characters.  Anything above that is indexed and, if it looks
#: thin next to its image count, merely flagged -- an uncertain scan must not
#: become a hard failure.
MIN_TEXT_CHARS = 1

#: Thresholds for the advisory "this might be a scan" warning.
SCAN_SUSPECT_MAX_CHARS = 200
SCAN_SUSPECT_MIN_IMAGES = 3


@runtime_checkable
class DocumentParser(Protocol):
    """Contract every parser implements."""

    name: str
    version: str

    def supports(self, file_path: Path) -> bool:
        """Whether this parser claims the file. Must not raise."""
        ...

    def parse(self, file_path: Path) -> ParsedDocument:
        """Parse the file or raise a :class:`DocumentParseError` subclass.

        Implementations never return a partially-empty result to signal
        failure -- an unreadable document raises.
        """
        ...


class BaseParser:
    """Shared behaviour for the concrete parsers.

    Subclasses implement :meth:`_parse`; this class wraps it so that *any*
    unexpected exception becomes a ``PARSE_FAILED`` ``DocumentParseError``.
    That is what keeps a single malformed file from escaping as an arbitrary
    library exception and killing an ingestion worker.
    """

    name: str = "base"
    version: str = "0"
    extensions: tuple[str, ...] = ()

    def supports(self, file_path: Path) -> bool:
        try:
            return file_path.suffix.lower() in self.extensions
        except Exception:  # pragma: no cover - defensive, suffix cannot raise
            return False

    def parse(self, file_path: Path) -> ParsedDocument:
        file_path = Path(file_path)
        try:
            doc = self._parse(file_path)
        except DocumentParseError:
            raise
        except Exception as exc:
            # Unknown cause -- do NOT invent a specific classification.
            raise ParseFailedError(
                f"{type(exc).__name__}: {exc}", detail=type(exc).__name__
            ) from exc
        self._classify_empty(doc)
        return doc

    # -- to be implemented by subclasses ----------------------------------

    def _parse(self, file_path: Path) -> ParsedDocument:  # pragma: no cover
        raise NotImplementedError

    # -- shared post-processing -------------------------------------------

    @staticmethod
    def _classify_empty(doc: ParsedDocument) -> None:
        """Decide between "fine", EMPTY_DOCUMENT and OCR_REQUIRED.

        The distinction rests on evidence from the file: ``metadata['image_count']``
        counts embedded binary/image objects the parser actually saw.  We only
        claim OCR_REQUIRED when there is no text *and* there are images.
        """
        text = "".join(p.text for p in doc.paragraphs)
        for table in doc.tables:
            for row in table.rows:
                text += "".join(row)
        meaningful = len(text.strip())
        image_count = int(doc.metadata.get("image_count") or 0)

        if meaningful >= MIN_TEXT_CHARS:
            # Many images but very little text is suspicious, yet not
            # conclusive -- warn instead of failing, and leave the OCR decision
            # to a human or a later pipeline stage.
            if (
                image_count >= SCAN_SUSPECT_MIN_IMAGES
                and meaningful < SCAN_SUSPECT_MAX_CHARS
            ):
                doc.add_warning("POSSIBLE_SCANNED_DOCUMENT")
            return

        if image_count > 0:
            raise OCRRequiredError(
                f"no extractable text ({meaningful} chars) but "
                f"{image_count} embedded image object(s) present"
            )
        raise EmptyDocumentError(f"no extractable text ({meaningful} chars) and no images")
