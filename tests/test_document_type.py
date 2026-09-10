"""Document-kind classification rules."""

from __future__ import annotations

import pytest

from ingestion.document_type import (
    LABELS,
    MANUAL,
    ORDER,
    OTHER,
    PROPOSAL,
    REPORT,
    REQUIREMENTS,
    TAG_NAMESPACE,
    all_tag_names,
    classify_filename,
    is_type_tag,
    normalize,
    type_for_file,
)


class TestNormalize:
    def test_separators_are_removed_so_naming_style_does_not_matter(self):
        forms = ["요구사항_정의서", "요구사항 정의서", "요구사항-정의서", "[요구사항]정의서"]
        assert len({normalize(f) for f in forms}) == 1

    def test_case_is_folded(self):
        assert normalize("MANUAL") == normalize("manual")

    def test_decomposed_korean_is_composed_first(self):
        """macOS types Korean decomposed; NFD text contains no composed keyword."""
        import unicodedata

        assert normalize(unicodedata.normalize("NFD", "매뉴얼")) == "매뉴얼"


class TestRealCorpus:
    """The eight files actually uploaded to the VM for verification."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("완료보고서_d251126.hwp", REPORT),
            ("사용자매뉴얼-윤수현.hwp", MANUAL),
            ("첨부 1. 제안요청서.hwp", PROPOSAL),
            ("요구사항_정의서_윤수현.hwp", REQUIREMENTS),
            ("요구사항_정의서_예시(KISTI).hwp", REQUIREMENTS),
            ("[KISTI]SMARTer고도화-매뉴얼.hwp", MANUAL),
            ("매뉴얼_윤수현.hwp", MANUAL),
            ("기능요구사항_정의서.docx", REQUIREMENTS),
        ],
    )
    def test_expected_classification(self, filename, expected):
        assert type_for_file(filename) == expected

    def test_none_of_them_fall_through_to_other(self):
        names = [
            "완료보고서_d251126.hwp", "사용자매뉴얼-윤수현.hwp", "첨부 1. 제안요청서.hwp",
            "요구사항_정의서_윤수현.hwp", "요구사항_정의서_예시(KISTI).hwp",
            "[KISTI]SMARTer고도화-매뉴얼.hwp", "매뉴얼_윤수현.hwp", "기능요구사항_정의서.docx",
        ]
        assert all(type_for_file(n) != OTHER for n in names)


class TestSpecificityWins:
    def test_longer_keyword_beats_shorter_one_of_another_kind(self):
        """"<kind> 최종보고서" is the common naming pattern.

        A bare "보고서" must not outrank the more specific kind keyword, or
        every requirements document filed as a report becomes a report.
        """
        result = classify_filename("요구사항정의서 최종보고서.hwp")
        assert result.code == REQUIREMENTS
        assert result.matched == "요구사항정의서"

    def test_more_specific_proposal_keyword_wins_over_generic_requirement(self):
        assert type_for_file("제안요청서 요구사항.hwp") == PROPOSAL

    def test_longest_keyword_within_one_kind_is_reported(self):
        assert classify_filename("사용자매뉴얼.hwp").matched == "사용자매뉴얼"


class TestConservativeDefault:
    def test_no_match_is_other(self):
        for name in ("사업계획.hwp", "회의록_20260101.hwp", "무제.hwp"):
            assert type_for_file(name) == OTHER

    def test_equally_specific_conflict_is_other_rather_than_a_coin_flip(self):
        """A wrong kind hides the document from that filter, silently."""
        result = classify_filename("제안서 보고서.hwp")
        assert result.code == OTHER
        assert result.reason.startswith("AMBIGUOUS")

    def test_empty_and_extension_only_names(self):
        assert type_for_file("") == OTHER
        assert type_for_file(".hwp") == OTHER

    def test_extension_never_decides_the_kind(self):
        # "guide" inside an extension-like tail must not classify anything.
        assert type_for_file("자료.guide") == OTHER


class TestTagNaming:
    def test_every_kind_has_a_namespaced_tag_name(self):
        names = all_tag_names()
        assert len(names) == len(ORDER) == 5
        assert all(name.startswith(TAG_NAMESPACE) for name in names)

    def test_tag_name_carries_the_display_label_verbatim(self):
        """The UI strips the prefix and shows the rest, so there is no second
        mapping table that could drift out of step."""
        result = classify_filename("사용자매뉴얼.hwp")
        assert result.tag_name == f"{TAG_NAMESPACE}매뉴얼"
        assert result.tag_name[len(TAG_NAMESPACE):] == LABELS[MANUAL]

    def test_free_form_tags_are_distinguishable(self):
        assert is_type_tag("종류:보고서")
        assert not is_type_tag("보안")
        assert not is_type_tag("2026년")

    def test_other_is_a_real_tag_not_an_absence(self):
        """"not classified yet" and "classified as 기타" must be tellable apart."""
        assert classify_filename("무제.hwp").tag_name == f"{TAG_NAMESPACE}기타"


class TestDeterminism:
    def test_same_name_always_gives_the_same_answer(self):
        name = "요구사항_정의서_윤수현.hwp"
        assert len({type_for_file(name) for _ in range(20)}) == 1

    def test_no_network_or_model_is_used(self):
        """The classifier must stay a pure function of the file name."""
        import inspect

        from ingestion import document_type

        source = inspect.getsource(document_type)
        for forbidden in ("requests", "http", "openai", "anthropic", "SentenceTransformer"):
            assert forbidden not in source
