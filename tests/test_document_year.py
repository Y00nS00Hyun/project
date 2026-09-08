"""Unit tests for document_year extraction.

The extractor decides what a `year=` search filter will match. A wrong year is
worse than no year: the document silently disappears from a filtered search and
nobody can tell it happened. These tests pin the conservative behaviour.
"""

from __future__ import annotations

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
