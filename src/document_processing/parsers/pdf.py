"""PDF parser for documents with a text layer.

Built on ``pypdf`` (BSD-3-Clause, no required dependencies on Python 3.11+).
It reads the text a PDF already carries; it does not render pages and does not
recognise characters in images. A scanned PDF therefore has no text here, and
is classified OCR_REQUIRED by the shared post-processing in :class:`BaseParser`
-- a determination about the file, not a worker failure.

**Pages.** Unlike HWP, HWPX and DOCX, a PDF has real pages, so every paragraph
carries ``page_number``: the 1-based position of the page in the file. That is
the page a reader reaches by scrolling, which is not always the number printed
on it (a document whose front matter is numbered i, ii, iii has page 4 labelled
"1"). Page labels are not consulted -- the position is the one a citation can
be checked against unambiguously.

**Blocks.** pypdf's plain extraction mode ends every rendered line with a
newline and, for most PDFs, leaves no blank line between paragraphs. Treating
each line as a paragraph would turn one page into dozens of fragments; treating
each page as one paragraph would give the chunker nothing but token counts to
split on. So a page is divided at exactly two places:

  * a blank line, where the extraction did preserve a paragraph gap
  * after a line that ends a sentence (``.``, ``?``, ``!`` and their full-width
    forms, optionally followed by a closing quote or bracket)

That is not layout analysis. It never reorders text, never merges lines across
pages, and never looks at coordinates; the reading order is whatever pypdf
produced. A heading with no final punctuation simply joins the paragraph after
it, which costs nothing -- the chunker packs neighbouring blocks back together
up to its budget anyway.

Out of scope: OCR, table structure, multi-column reconstruction, annotations,
attachments, page labels, and document dates from PDF metadata (a PDF's
CreationDate is when the file was produced, which is often not when the
document was written; dates come from the text, as for every other format).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..models import ParsedDocument, ParsedParagraph
from .base import BaseParser
from .exceptions import CorruptDocumentError, EncryptedDocumentError, ParseFailedError

PARSER_NAME = "inhouse-pdf"
PARSER_VERSION = "0.1.0"

_BLANK_LINE = re.compile(r"\n[ \t\f\v　]*\n")
#: A line that ends a sentence. Closing quotes and brackets may follow the mark.
_SENTENCE_END = re.compile(r"[.?!。．？！][\"'”’)\]」』）]*\s*$")


def split_blocks(page_text: str) -> list[str]:
    """Divide one page's extracted text into paragraph-sized blocks.

    Public so the rule can be tested on its own, without building a PDF.
    """
    text = page_text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    for section in _BLANK_LINE.split(text):
        current: list[str] = []
        for line in section.split("\n"):
            if not line.strip():
                continue
            current.append(line.strip())
            if _SENTENCE_END.search(line):
                blocks.append("\n".join(current))
                current = []
        if current:
            blocks.append("\n".join(current))
    return blocks


class PdfParser(BaseParser):
    """Parses text-layer PDFs into a :class:`ParsedDocument`."""

    name = PARSER_NAME
    version = PARSER_VERSION
    extensions = (".pdf",)

    def _parse(self, file_path: Path) -> ParsedDocument:
        try:
            from pypdf import PdfReader
            from pypdf.errors import DependencyError, PdfReadError
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ParseFailedError(
                "pypdf is not installed", detail="MISSING_DEPENDENCY"
            ) from exc

        try:
            # strict=False: tolerate the minor spec violations real-world
            # producers leave behind, rather than refusing an otherwise
            # readable file.
            reader = PdfReader(str(file_path), strict=False)
        except PdfReadError as exc:
            # No header, no EOF marker, an unreadable cross-reference table: the
            # file is not a PDF pypdf can open at all.
            raise CorruptDocumentError(
                f"not a readable PDF: {exc}", detail=type(exc).__name__
            ) from exc

        self._open_encryption(reader, DependencyError)

        doc = ParsedDocument(
            file_path=str(file_path),
            file_type="pdf",
            parser_name=self.name,
            parser_version=self.version,
        )

        try:
            pages = list(reader.pages)
        except PdfReadError as exc:
            raise CorruptDocumentError(
                f"page tree unreadable: {exc}", detail=type(exc).__name__
            ) from exc
        doc.page_count = len(pages)

        unreadable = 0
        image_count = 0
        for number, page in enumerate(pages, start=1):
            image_count += self._count_images(page)
            try:
                page_text = page.extract_text() or ""
            except Exception:
                # One damaged content stream should not cost the rest of the
                # document. The warning is the record that a page was lost.
                unreadable += 1
                doc.add_warning("PAGE_TEXT_UNREADABLE")
                continue
            for block in split_blocks(page_text):
                doc.paragraphs.append(
                    ParsedParagraph(
                        index=len(doc.paragraphs),
                        text=block,
                        page_number=number,
                        paragraph_type="body",
                    )
                )

        if pages and unreadable == len(pages):
            # Every page failed to decode. That is a damaged file, not a PDF
            # without a text layer, and must not be reported as OCR_REQUIRED.
            raise CorruptDocumentError(
                "no page could be read", detail="NO_PAGE_READABLE"
            )

        doc.metadata["image_count"] = image_count
        return doc

    @staticmethod
    def _open_encryption(reader: Any, dependency_error: type[Exception]) -> None:
        """Open an encrypted PDF if it needs no password, else refuse it.

        Many "encrypted" PDFs only carry an owner password restricting printing
        or copying and open with an empty user password; those are read
        normally. A PDF that needs a password we do not have is ENCRYPTED --
        the same determination the HWP parsers make.

        AES-encrypted files need a crypto library pypdf does not bundle. None is
        installed, so such a file is also reported ENCRYPTED, with a detail
        saying why, rather than as a crash.
        """
        if not reader.is_encrypted:
            return
        try:
            result = reader.decrypt("")
        except dependency_error as exc:
            raise EncryptedDocumentError(
                f"encrypted with an algorithm that needs a crypto library: {exc}",
                detail="CRYPTO_LIBRARY_REQUIRED",
            ) from exc
        except Exception as exc:
            raise EncryptedDocumentError(
                f"encryption could not be opened: {exc}", detail=type(exc).__name__
            ) from exc
        if not result:  # PasswordType.NOT_DECRYPTED == 0
            raise EncryptedDocumentError(
                "a password is required to open this PDF", detail="USER_PASSWORD_REQUIRED"
            )

    @staticmethod
    def _count_images(page: Any) -> int:
        """Image XObjects a page draws from its resources.

        Feeds BaseParser's empty/OCR classification: no text plus images means
        OCR_REQUIRED rather than EMPTY_DOCUMENT. Counts the page's own XObject
        dictionary without decoding any image data; images nested inside form
        XObjects or written inline are not counted -- the question is only
        whether the file carries pictures at all.
        """
        try:
            resources = page.get("/Resources")
            resources = resources.get_object() if resources is not None else None
            xobjects = resources.get("/XObject") if resources is not None else None
            xobjects = xobjects.get_object() if xobjects is not None else None
            if not xobjects:
                return 0
            return sum(
                1 for ref in xobjects.values()
                if ref.get_object().get("/Subtype") == "/Image"
            )
        except Exception:
            return 0
