"""Deterministic document-type classification from the file name.

One of five fixed kinds, decided by rules over the file name alone. No LLM, no
body text, no network -- the same name always yields the same kind, and the
result can be explained to a user by pointing at the file name.

The conservative default matters as much as the rules. A document filed under
the wrong kind disappears from a kind-filtered search and the user has no way
to notice, so anything the rules cannot settle becomes OTHER rather than a
guess. This mirrors ``document_year``, which leaves NULL for the same reason.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Reserved prefix separating document-kind tags from free-form tags.
#: Free-form tag creation must reject names starting with this.
TAG_NAMESPACE = "종류:"

MANUAL = "MANUAL"
REQUIREMENTS = "REQUIREMENTS"
PROPOSAL = "PROPOSAL"
REPORT = "REPORT"
OTHER = "OTHER"

#: The label a user sees. Stored after the namespace prefix so the tag name is
#: readable on its own and the UI only has to strip the prefix -- there is no
#: second mapping table to keep in step.
LABELS = {
    MANUAL: "매뉴얼",
    REQUIREMENTS: "요구사항 정의서",
    PROPOSAL: "제안·입찰 문서",
    REPORT: "보고서",
    OTHER: "기타",
}

#: Display order for the filter, most specific kinds first and OTHER last.
ORDER = (MANUAL, REQUIREMENTS, PROPOSAL, REPORT, OTHER)

# ---------------------------------------------------------------------------
# Keywords
#
# Matched against the file name with separators stripped, so "요구사항_정의서"
# and "요구사항 정의서" behave identically. Longer keywords are more specific
# and win over shorter ones -- "요구사항정의서" beats a bare "보고서" -- which
# is what resolves the common "<kind> 최종보고서" naming pattern.
# ---------------------------------------------------------------------------
KEYWORDS: dict[str, tuple[str, ...]] = {
    REQUIREMENTS: (
        "요구사항정의서", "기능요구사항정의서", "요구사항명세서", "요구사항명세",
        "기능요구사항", "요구사항", "기능정의서",
        "requirementspecification", "requirements", "requirement",
    ),
    PROPOSAL: (
        "제안요청서", "과업지시서", "입찰공고", "제안평가", "제안서",
        "견적요청서", "입찰", "낙찰", "rfp",
    ),
    REPORT: (
        "완료보고서", "착수보고서", "중간보고서", "결과보고서", "최종보고서",
        "수행보고서", "완료보고", "착수보고", "보고서", "report",
    ),
    MANUAL: (
        "사용자매뉴얼", "설치매뉴얼", "운영매뉴얼", "관리자매뉴얼",
        "사용자가이드", "사용설명서", "설치가이드", "매뉴얼", "안내서",
        "manual", "guide",
    ),
}

_SEPARATORS = re.compile(r"[\s_\-.,\[\]()<>~!@#$%^&+=|/\\'\"`:;]+")


def normalize(text: str) -> str:
    """Fold a name down to the form keywords are matched against.

    NFC first: Korean typed on macOS arrives decomposed, and "매뉴얼" in NFD
    would not contain the composed keyword at all.
    """
    return _SEPARATORS.sub("", unicodedata.normalize("NFC", text).lower())


@dataclass(frozen=True)
class TypeClassification:
    """The decision plus why, so a surprising result can be explained."""

    code: str
    reason: str
    #: The keyword that decided it, for logging and tests. None for OTHER.
    matched: str | None = None

    @property
    def label(self) -> str:
        return LABELS[self.code]

    @property
    def tag_name(self) -> str:
        return TAG_NAMESPACE + self.label


def classify_filename(filename: str) -> TypeClassification:
    """Classify a file name into exactly one kind.

    1. Strip the extension and normalize.
    2. For each kind, take its longest matching keyword.
    3. The kind with the longest match wins.
    4. A tie between different kinds is genuinely ambiguous -> OTHER.
    5. No match -> OTHER.
    """
    if not filename:
        return TypeClassification(OTHER, "EMPTY_FILENAME")

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    haystack = normalize(stem)
    if not haystack:
        return TypeClassification(OTHER, "EMPTY_FILENAME")

    # Longest keyword per kind.
    best: dict[str, str] = {}
    for code, keywords in KEYWORDS.items():
        hits = [kw for kw in keywords if normalize(kw) in haystack]
        if hits:
            best[code] = max(hits, key=lambda kw: len(normalize(kw)))

    if not best:
        return TypeClassification(OTHER, "NO_KEYWORD_MATCH")

    longest = max(len(normalize(kw)) for kw in best.values())
    winners = [code for code, kw in best.items() if len(normalize(kw)) == longest]

    if len(winners) > 1:
        # Two kinds matched with equally specific keywords. Picking either is a
        # coin flip, and the wrong side hides the document from that filter.
        return TypeClassification(
            OTHER, "AMBIGUOUS_" + "_".join(sorted(winners))
        )

    code = winners[0]
    return TypeClassification(code, "KEYWORD_MATCH", best[code])


def type_for_file(filename: str) -> str:
    """Just the code, for callers that do not need the reasoning."""
    return classify_filename(filename).code


def all_tag_names() -> tuple[str, ...]:
    """Every document-kind tag name, in display order."""
    return tuple(TAG_NAMESPACE + LABELS[code] for code in ORDER)


def is_type_tag(tag_name: str) -> bool:
    return tag_name.startswith(TAG_NAMESPACE)
