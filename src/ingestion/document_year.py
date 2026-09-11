"""Deterministic document-year extraction.

``document_revisions.document_year`` means *the year the revision's content is
about* -- "2026년 사업계획서" is 2026. It is explicitly NOT the file's mtime nor
the row's creation time: a 2026 plan may be edited in 2027 and a 2019 document
may be discovered today.

Where the year is looked for
---------------------------
The cover and the first few blocks of the parsed text, then the file name --
never the whole document. Scanning the body is actively wrong: the completion
report in the verification corpus mentions 2008, 2015, 2016 and 2025, and the
proposal request mentions six different years across its clauses. Only the
front matter carries the date the document is *about*.

The extractor is rule-based and conservative. When it cannot be confident the
answer is NULL, because a wrong year silently removes a document from a
``year=`` filtered search and the user has no way to notice.

No NLP, no OCR and no LLM: only text the parser already produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

#: Schema CHECK: document_year IS NULL OR BETWEEN 1900 AND 2100.
MIN_YEAR = 1900
MAX_YEAR = 2100

# ---------------------------------------------------------------------------
# Front matter
#
# `extracted_text` is a sequence of "[문단]" and "[표]" blocks, so a block is
# the natural unit of "the first few paragraphs" -- and it keeps cover tables,
# which is where a Korean document's date usually sits.
#
# 20 blocks / 2000 chars: measured on the verification corpus, where the
# longest front matter (the completion report's cover, document-information
# table and revision history) runs 10 blocks and 596 characters before the
# table of contents. Twice that is comfortable headroom, and the character cap
# bounds the scan on a 200,000-character report. Sweeping 10/15/20/30 blocks
# over that corpus produced identical answers, so the exact value is not
# load-bearing.
# ---------------------------------------------------------------------------
FRONT_MATTER_BLOCKS = 20
FRONT_MATTER_CHARS = 2000

_BLOCK_MARKER = re.compile(r"^\[(?:문단|표)\]$", re.M)

#: A run of exactly four digits with no digit on either side, so a date-like
#: token (20260908) or an ID (1202612) never contributes a bogus year.
_FOUR_DIGIT = re.compile(r"(?<![0-9])(\d{4})(?![0-9])")

#: Full dates. The year is matched as any four digits and range-checked
#: afterwards, so 2100 (the schema's upper bound) is not excluded by the
#: pattern itself. Covers "2025. 11. 26.", "2025.11.26", "2025-11-26",
#: "2025/11/26" and "2025년 11월 26일". The separators may be spaced.
_FULL_DATE = re.compile(
    r"(?<![0-9])(?P<y1>\d{4})\s*[.\-/]\s*(?P<m1>\d{1,2})\s*[.\-/]\s*(?P<d1>\d{1,2})(?![0-9])"
    r"|(?<![0-9])(?P<y2>\d{4})\s*년\s*(?P<m2>\d{1,2})\s*월\s*(?P<d2>\d{1,2})\s*일"
)


@dataclass(frozen=True)
class YearExtraction:
    """The decision, plus why -- so a surprising year can be explained."""

    year: int | None
    reason: str
    #: The exact text the year came from. None when nothing matched.
    evidence: str | None = None
    #: The full date the front matter states, when it states exactly one.
    #:
    #: Decided separately from ``year`` and not derivable from it. A cover
    #: carrying "2025.01.05" and "2025.12.20" states one year and two dates:
    #: year is 2025, date is None. The reverse never happens -- a date always
    #: fixes a year -- so ``date`` being set implies ``year`` is too.
    #:
    #: Never synthesised. A bare "2026년도" yields year 2026 and date None,
    #: because 2026-01-01 is a fact the document does not state.
    date: date | None = None


def _in_range(year: int) -> bool:
    return MIN_YEAR <= year <= MAX_YEAR


def front_matter(text: str) -> str:
    """The leading portion of parsed text a cover date could plausibly sit in.

    Cut at whichever comes first: the start of the 21st block, or 2000
    characters. Returns "" for empty input.
    """
    if not text:
        return ""
    starts = [m.start() for m in _BLOCK_MARKER.finditer(text)]
    end = starts[FRONT_MATTER_BLOCKS] if len(starts) > FRONT_MATTER_BLOCKS else len(text)
    return text[: min(end, FRONT_MATTER_CHARS)]


def _full_dates(text: str) -> list[tuple[date, str]]:
    """Every valid full date in ``text`` as (date, matched text).

    The calendar decides what is valid: ``date(...)`` rejects 2025-02-30 and
    2025-13-01 on its own, which is stricter than a range check and is the same
    rule the DATE column will apply. A string that looks like a date but is not
    one is simply not a date, and treating it as evidence would put a day on a
    document that never stated it.
    """
    found: list[tuple[date, str]] = []
    for match in _FULL_DATE.finditer(text):
        year = int(match.group("y1") or match.group("y2"))
        month = int(match.group("m1") or match.group("m2"))
        day = int(match.group("d1") or match.group("d2"))
        if not _in_range(year):
            continue
        try:
            found.append((date(year, month, day), " ".join(match.group(0).split())))
        except ValueError:
            continue
    return found


def _standalone_years(text: str) -> list[tuple[int, str]]:
    return [
        (int(m.group(1)), m.group(1))
        for m in _FOUR_DIGIT.finditer(text)
        if _in_range(int(m.group(1)))
    ]


def _decide(candidates: list[tuple[int, str]], found: str, ambiguous: str) -> YearExtraction | None:
    """One distinct year -> take it. Several -> refuse. None -> keep looking."""
    if not candidates:
        return None
    years = {year for year, _ in candidates}
    if len(years) > 1:
        # "2025-2026 사업계획", or a cover naming one year and a document-info
        # table naming another. Picking either is a coin flip, and the wrong
        # side hides the document from that year's filter.
        return YearExtraction(None, ambiguous, ", ".join(sorted(str(y) for y in years)))
    year, evidence = candidates[0]
    return YearExtraction(year, found, evidence)


def _decide_date(dates: list[tuple[date, str]]) -> date | None:
    """One distinct date -> take it. Several, or none -> None.

    Distinct *dates*, not distinct years. The same day written twice in two
    formats -- "2025. 11. 26." on the cover and "2025.11.26" in the document
    table -- is one date, and that is the common case. Two different days in
    the same year is one year and no date.
    """
    distinct = {value for value, _ in dates}
    return distinct.pop() if len(distinct) == 1 else None


def extract_from_front_matter(text: str) -> YearExtraction:
    """Priorities 1 and 2: the parsed document's own front matter.

    A full date outranks a bare year even when the bare year appears first: a
    cover reading "2025년도 사업" above "작성일 2026.01.15" is a 2026 document
    about the 2025 fiscal year, and the explicit date is the stronger signal.
    """
    window = front_matter(text)
    if not window:
        return YearExtraction(None, "NO_TEXT")

    dates = _full_dates(window)
    # Carried onto whichever decision follows. Unambiguous dates settle the
    # year too; ambiguous ones settle neither, and a year found further down
    # the chain is not evidence of a day.
    exact_date = _decide_date(dates)

    decided = _decide(
        [(value.year, evidence) for value, evidence in dates],
        "FULL_DATE_IN_FRONT_MATTER", "AMBIGUOUS_FULL_DATES",
    )
    if decided is not None:
        return YearExtraction(decided.year, decided.reason, decided.evidence, exact_date)

    decided = _decide(
        _standalone_years(window), "YEAR_IN_FRONT_MATTER", "AMBIGUOUS_YEARS_IN_FRONT_MATTER"
    )
    if decided is not None:
        return decided

    return YearExtraction(None, "NO_YEAR_IN_FRONT_MATTER")


def extract_document_year(title: str) -> YearExtraction:
    """Priority 3: a single unambiguous four-digit year in the file name."""
    if not title:
        return YearExtraction(None, "EMPTY_TITLE")

    decided = _decide(
        _standalone_years(title), "SINGLE_YEAR_IN_TITLE", "AMBIGUOUS_MULTIPLE_YEARS"
    )
    if decided is not None:
        return decided
    return YearExtraction(None, "NO_YEAR_IN_RANGE")


def extract_year(title: str, extracted_text: str | None = None) -> YearExtraction:
    """The full priority chain.

    1. an explicit full date in the parsed front matter
    2. an explicit four-digit year in the parsed front matter
    3. a single explicit four-digit year in the file name
    4. otherwise NULL

    An *ambiguous* front matter stops the chain rather than falling through to
    the file name. Two conflicting dates on a cover mean the document does not
    state one year, and a year inferred from the file name would contradict
    what the document itself says.
    """
    if extracted_text:
        from_text = extract_from_front_matter(extracted_text)
        if from_text.year is not None or from_text.reason.startswith("AMBIGUOUS"):
            return from_text
    return extract_document_year(title)


def year_for_file(title: str) -> int | None:
    """File name only. Used at discovery time, before the parser has run."""
    return extract_document_year(title).year


def year_for_document(title: str, extracted_text: str | None = None) -> int | None:
    return extract_year(title, extracted_text).year


def date_for_document(title: str, extracted_text: str | None = None) -> date | None:
    """The date the document states, or None.

    Only the front matter can produce one. A file name never does: "d251126"
    is not a date expression, and reading it as one would be exactly the guess
    the whole extractor refuses to make.
    """
    return extract_year(title, extracted_text).date
