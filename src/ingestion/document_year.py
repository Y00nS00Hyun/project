"""Deterministic document-year extraction.

``document_revisions.document_year`` means *the year the revision's content is
about* -- "2026년 사업계획서" is 2026. It is explicitly NOT the file's mtime nor
the row's creation time: a 2026 plan may be edited in 2027 and a 2019 document
may be discovered today.

The extractor is rule-based and conservative. When it cannot be confident the
answer is NULL, because a wrong year silently removes a document from a
`year=` filtered search and the user has no way to notice.

No NLP or LLM is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Schema CHECK: document_year IS NULL OR BETWEEN 1900 AND 2100.
MIN_YEAR = 1900
MAX_YEAR = 2100

#: A run of exactly four digits with no digit on either side, so a date-like
#: token (20260908) or an ID (1202612) never contributes a bogus year.
_FOUR_DIGIT = re.compile(r"(?<!\d)(\d{4})(?!\d)")


@dataclass(frozen=True)
class YearExtraction:
    """Result plus the reason, so behaviour can be explained and tested.

    The reason is not persisted -- there is no provenance column and this stage
    does not add one (see docs/ingestion-foundation.md, Architecture Notes).
    """

    year: int | None
    reason: str

    @property
    def found(self) -> bool:
        return self.year is not None


def extract_document_year(title: str) -> YearExtraction:
    """Extract a single unambiguous year from a document title.

    Rules, in order:

    1. Collect every standalone 4-digit run in ``1900..2100``.
    2. Exactly one distinct candidate -> that year.
    3. More than one distinct candidate -> NULL (ambiguous, e.g. "2025-2026").
    4. No candidate -> NULL.

    Out-of-range numbers are simply not candidates, so "1800년 자료" yields NULL
    rather than 1800.
    """
    if not title:
        return YearExtraction(None, "EMPTY_TITLE")

    candidates = {
        int(match.group(1))
        for match in _FOUR_DIGIT.finditer(title)
        if MIN_YEAR <= int(match.group(1)) <= MAX_YEAR
    }

    if not candidates:
        return YearExtraction(None, "NO_YEAR_IN_RANGE")
    if len(candidates) > 1:
        # "2025-2026 사업계획": picking either one would be a guess, and the
        # wrong guess hides the document from a year filter.
        return YearExtraction(None, "AMBIGUOUS_MULTIPLE_YEARS")
    return YearExtraction(candidates.pop(), "SINGLE_YEAR_IN_TITLE")


def year_for_file(title: str) -> int | None:
    """Convenience wrapper returning just the value stored in the column."""
    return extract_document_year(title).year
