"""Unit tests for document_year extraction.

The extractor decides what a `year=` search filter will match. A wrong year is
worse than no year: the document silently disappears from a filtered search and
nobody can tell it happened. These tests pin the conservative behaviour.
"""

from __future__ import annotations

from datetime import date

import pytest

from ingestion.document_year import (
    MAX_YEAR,
    MIN_YEAR,
    extract_document_year,
    year_for_file,
)


class TestExplicitYear:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("2026년 사업계획서", 2026),
            ("2025_보안운영지침", 2025),
            ("2024-정보화-추진계획", 2024),
            ("[2023] 감사보고서", 2023),
            ("사업계획 2026", 2026),
            ("2026", 2026),
        ],
    )
    def test_single_year_is_extracted(self, title, expected):
        assert year_for_file(title) == expected

    def test_repeated_same_year_is_not_ambiguous(self):
        # One distinct value, mentioned twice.
        assert year_for_file("2026년 사업계획 최종(2026)") == 2026

    def test_reason_is_reported(self):
        assert extract_document_year("2026년 계획").reason == "SINGLE_YEAR_IN_TITLE"


class TestAmbiguousOrAbsent:
    @pytest.mark.parametrize(
        "title",
        [
            "회의록_최종",
            "붙임1 서식",
            "",
            "업무보고",
        ],
    )
    def test_no_year_yields_none(self, title):
        assert year_for_file(title) is None

    def test_multiple_distinct_years_yield_none(self):
        # Picking either end of "2025-2026" would be a guess.
        result = extract_document_year("2025-2026 사업계획")
        assert result.year is None
        assert result.reason == "AMBIGUOUS_MULTIPLE_YEARS"

    def test_three_years_yield_none(self):
        assert year_for_file("2024 2025 2026 비교자료") is None


class TestRangeGuard:
    @pytest.mark.parametrize("title", ["1800년 자료", "3000년 계획", "0001 문서"])
    def test_out_of_range_years_are_not_candidates(self, title):
        assert year_for_file(title) is None

    @pytest.mark.parametrize("year", [MIN_YEAR, MAX_YEAR])
    def test_range_boundaries_are_inclusive(self, year):
        assert year_for_file(f"{year}년 문서") == year

    @pytest.mark.parametrize("year", [MIN_YEAR - 1, MAX_YEAR + 1])
    def test_just_outside_the_range_is_rejected(self, year):
        assert year_for_file(f"{year}년 문서") is None

    def test_extracted_year_always_satisfies_the_db_check(self):
        # document_year SMALLINT CHECK (NULL OR BETWEEN 1900 AND 2100)
        for title in ["2026년", "1899년", "2101년", "회의록", "2025-2026"]:
            value = year_for_file(title)
            assert value is None or MIN_YEAR <= value <= MAX_YEAR


class TestDigitBoundaries:
    def test_long_digit_runs_do_not_produce_a_year(self):
        # A timestamp-like token must not contribute 2026.
        assert year_for_file("20260908_백업") is None

    def test_year_inside_a_longer_number_is_ignored(self):
        assert year_for_file("문서번호 1202612") is None

    def test_document_number_with_one_in_range_year(self):
        # "제2025-477호": 477 is out of range, so 2025 is the only candidate.
        # Accepted deliberately -- a document number year is usually the
        # document's year, and the alternative (dropping it) loses real signal.
        assert year_for_file("제2025-477호 공고") == 2025

    def test_source_mtime_is_never_consulted(self):
        # The extractor takes a title only; there is no filesystem input it
        # could fall back to.
        assert year_for_file("회의록") is None


# ---------------------------------------------------------------------------
# Front matter takes priority over the file name (policy change).
# ---------------------------------------------------------------------------

from ingestion.document_year import (  # noqa: E402
    FRONT_MATTER_BLOCKS,
    FRONT_MATTER_CHARS,
    extract_from_front_matter,
    extract_year,
    front_matter,
    year_for_document,
)

COVER = "[표]\n | | \n완료 보고서 Project Finished Report | | \n[문단]\n{date}\n[문단]\n주식회사 예시\n"


class TestFrontMatterWindow:
    def test_stops_at_the_block_limit(self):
        text = "".join(f"[문단]\n블록 {i}\n" for i in range(60))
        window = front_matter(text)
        assert "블록 0" in window
        assert f"블록 {FRONT_MATTER_BLOCKS + 5}" not in window

    def test_stops_at_the_character_limit(self):
        # One enormous cover table must not drag the whole document in.
        text = "[표]\n" + ("가" * 50_000)
        assert len(front_matter(text)) <= FRONT_MATTER_CHARS

    def test_empty_text_is_not_an_error(self):
        assert front_matter("") == ""
        assert extract_from_front_matter("").year is None


class TestFullDatePatterns:
    @pytest.mark.parametrize(
        "date",
        ["2025. 11. 26.", "2025.11.26", "2025-11-26", "2025/11/26", "2025년 11월 26일"],
    )
    def test_every_required_pattern_is_recognised(self, date):
        result = extract_from_front_matter(COVER.format(date=date))
        assert result.year == 2025
        assert result.reason == "FULL_DATE_IN_FRONT_MATTER"

    def test_an_impossible_date_is_not_a_date(self):
        """2025.13.45 is not a date; it must not be read as one."""
        result = extract_from_front_matter(COVER.format(date="2025.13.45"))
        assert result.reason != "FULL_DATE_IN_FRONT_MATTER"

    def test_a_year_month_without_a_day_falls_back_to_the_year_rule(self):
        # "2026. 9." is how the verification corpus's proposal request is dated.
        result = extract_from_front_matter("[문단]\n제 안 요 청 서\n[문단]\n2026. 9.\n")
        assert result.year == 2026
        assert result.reason == "YEAR_IN_FRONT_MATTER"


class TestPriorityOrder:
    def test_front_matter_full_date_beats_the_file_name(self):
        """The headline case: the report's name says d251126, its cover says 2025."""
        result = extract_year("완료보고서_d251126", COVER.format(date="2025. 11. 26."))
        assert result.year == 2025
        assert result.reason == "FULL_DATE_IN_FRONT_MATTER"
        assert "2025" in (result.evidence or "")

    def test_front_matter_full_date_beats_a_year_in_the_file_name(self):
        result = extract_year("2019년 계획서", COVER.format(date="2025-11-26"))
        assert result.year == 2025

    def test_full_date_beats_a_standalone_year_in_the_same_front_matter(self):
        """A 2026 report about the 2025 fiscal year is a 2026 document."""
        text = "[문단]\n2025년도 사업\n[문단]\n작성일 2026.01.15\n"
        result = extract_from_front_matter(text)
        assert result.year == 2026
        assert result.reason == "FULL_DATE_IN_FRONT_MATTER"

    def test_standalone_year_in_front_matter_beats_the_file_name(self):
        result = extract_year("2019_자료", "[문단]\n제안요청서\n[문단]\n2026. 9.\n")
        assert result.year == 2026
        assert result.reason == "YEAR_IN_FRONT_MATTER"

    def test_file_name_is_used_when_the_front_matter_has_none(self):
        result = extract_year("2026년 사업계획서", "[문단]\n목적\n[문단]\n본 문서는...\n")
        assert result.year == 2026
        assert result.reason == "SINGLE_YEAR_IN_TITLE"

    def test_null_when_neither_has_a_year(self):
        assert year_for_document("매뉴얼", "[문단]\n설치 절차\n") is None


class TestAmbiguity:
    def test_conflicting_full_dates_yield_null(self):
        text = "[문단]\n2024.01.01\n[문단]\n2025.12.31\n"
        result = extract_from_front_matter(text)
        assert result.year is None
        assert result.reason == "AMBIGUOUS_FULL_DATES"

    def test_the_same_date_written_twice_is_not_a_conflict(self):
        # The corpus report carries "2025. 11. 26." on the cover and
        # "2025.11.26" in its document-information table.
        text = "[문단]\n2025. 11. 26.\n[표]\n작 성 일 | 2025.11.26\n"
        assert extract_from_front_matter(text).year == 2025

    def test_conflicting_standalone_years_yield_null(self):
        result = extract_from_front_matter("[문단]\n2024-2025 중기계획\n")
        assert result.year is None
        assert result.reason == "AMBIGUOUS_YEARS_IN_FRONT_MATTER"

    def test_ambiguous_front_matter_does_not_fall_through_to_the_file_name(self):
        """The document contradicts itself; the file name cannot settle it.

        Falling through would assert a year the document never states.
        """
        result = extract_year("2019년 보고서", "[문단]\n2024.01.01\n[문단]\n2025.12.31\n")
        assert result.year is None
        assert result.reason == "AMBIGUOUS_FULL_DATES"


class TestBodyIsNotSearched:
    def test_years_deep_in_the_body_are_ignored(self):
        """The corpus report mentions 2008, 2015 and 2016 in its body.

        Scanning the whole document would make the year ambiguous, or worse,
        pick a year the document is not about.
        """
        body = "".join(f"[문단]\n{y}년 관련 내용\n" for y in (2008, 2015, 2016))
        text = COVER.format(date="2025. 11. 26.") + body * 40
        assert extract_from_front_matter(text).year == 2025

    def test_a_year_only_in_the_body_is_not_used(self):
        text = "[문단]\n개요\n" + "[문단]\n채움\n" * 40 + "[문단]\n2013년 자료\n"
        assert extract_from_front_matter(text).year is None


class TestBackwardCompatibility:
    def test_year_for_file_still_reads_the_file_name_only(self):
        assert year_for_file("2026년 사업계획서") == 2026
        assert year_for_file("완료보고서_d251126") is None

    def test_schema_range_is_still_enforced(self):
        assert extract_from_front_matter("[문단]\n1800.01.01\n").year is None
        assert MIN_YEAR == 1900 and MAX_YEAR == 2100


# ---------------------------------------------------------------------------
# The full date the document states (migration 0007)
# ---------------------------------------------------------------------------

class TestDocumentDate:
    """`document_date` is what the cover prints, or nothing.

    It is decided separately from the year and is never synthesised from one:
    a document that states 2026 and no day has a year and no date, because
    2026-01-01 would be a fact the document does not contain.
    """

    def test_a_cover_date_is_extracted_whole(self):
        extraction = extract_year("완료보고서", "[문단]\n완료 보고서\n[문단]\n2025. 11. 26.")
        assert extraction.year == 2025
        assert extraction.date == date(2025, 11, 26)

    @pytest.mark.parametrize("text", [
        "[문단]\n2025. 11. 26.",
        "[문단]\n2025.11.26",
        "[문단]\n2025-11-26",
        "[문단]\n2025/11/26",
        "[문단]\n2025년 11월 26일",
    ])
    def test_every_separator_the_year_extractor_accepts(self, text):
        assert extract_year("보고서", text).date == date(2025, 11, 26)

    def test_the_same_day_written_two_ways_is_one_date(self):
        # The real completion report's front matter carries "2025. 11. 26." on
        # the cover and "2025.11.26" in its document-information table.
        extraction = extract_year(
            "완료보고서", "[문단]\n2025. 11. 26.\n[표]\n작성일 2025.11.26"
        )
        assert extraction.date == date(2025, 11, 26)

    def test_two_different_days_in_one_year_give_a_year_and_no_date(self):
        extraction = extract_year("보고서", "[문단]\n2025.01.05 착수\n[문단]\n2025.12.20 완료")
        assert extraction.year == 2025
        assert extraction.date is None

    def test_a_year_alone_never_becomes_a_date(self):
        extraction = extract_year("제안요청서", "[문단]\n2026년도 사업\n[문단]\n주관기관")
        assert extraction.year == 2026
        assert extraction.date is None

    def test_a_year_in_the_file_name_never_becomes_a_date(self):
        extraction = extract_year("2026_사업계획서", None)
        assert extraction.year == 2026
        assert extraction.date is None

    @pytest.mark.parametrize("text, expected_year", [
        ("[문단]\n2025.02.30 작성", 2025),   # February has no 30th
        ("[문단]\n2025.13.01 작성", 2025),   # no 13th month
    ])
    def test_an_impossible_date_is_not_a_date(self, text, expected_year):
        # The calendar decides. Falling back to the bare year is right: the
        # document does contain "2025", it just does not contain a valid day.
        extraction = extract_year("보고서", text)
        assert extraction.date is None
        assert extraction.year == expected_year

    def test_a_date_is_only_looked_for_in_the_front_matter(self):
        # Same rule as the year. A date deep in the body is a date the document
        # mentions, not the date the document was written.
        body = "[문단]\n표지\n" + "[문단]\n본문\n" * 60 + "[문단]\n2019.03.15 회의록"
        assert extract_year("보고서", body).date is None

    def test_date_for_document_is_the_same_answer(self):
        from ingestion.document_year import date_for_document

        text = "[문단]\n2025. 11. 26."
        assert date_for_document("보고서", text) == date(2025, 11, 26)
        assert date_for_document("보고서", None) is None
