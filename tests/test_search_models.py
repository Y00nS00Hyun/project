"""Unit tests for the search request/result contract. No DB, no model."""

from __future__ import annotations

import pytest

from search.exceptions import InvalidSearchModeError, InvalidSearchRequestError
from search.models import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SNIPPET_MAX_CHARS,
    DocumentResult,
    SearchMode,
    SearchRequest,
    make_snippet,
)


class TestSearchMode:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("default", SearchMode.DEFAULT),
            ("semantic", SearchMode.SEMANTIC),
            ("lexical", SearchMode.LEXICAL),
            ("SEMANTIC", SearchMode.SEMANTIC),
            (None, SearchMode.DEFAULT),
            ("", SearchMode.DEFAULT),
        ],
    )
    def test_parse(self, value, expected):
        assert SearchMode.parse(value) is expected

    @pytest.mark.parametrize("value", ["rrf", "hybrid", "vector", "fulltext", "  "])
    def test_unknown_mode_is_a_validation_error(self, value):
        with pytest.raises(InvalidSearchModeError):
            SearchMode.parse(value)

    def test_error_lists_the_allowed_modes(self):
        with pytest.raises(InvalidSearchModeError, match="semantic"):
            SearchMode.parse("nope")

    def test_only_three_modes_exist(self):
        assert {m.value for m in SearchMode} == {"default", "semantic", "lexical"}


class TestSearchRequest:
    def test_defaults(self):
        request = SearchRequest(user_id="u1")
        assert request.mode is SearchMode.DEFAULT
        assert request.page == 1
        assert request.size == DEFAULT_PAGE_SIZE
        assert request.tag_ids == ()

    def test_user_id_is_required(self):
        with pytest.raises(InvalidSearchRequestError):
            SearchRequest(user_id="")

    @pytest.mark.parametrize("page", [0, -1])
    def test_page_must_be_positive(self, page):
        with pytest.raises(InvalidSearchRequestError):
            SearchRequest(user_id="u1", page=page)

    @pytest.mark.parametrize("size", [0, -1, MAX_PAGE_SIZE + 1])
    def test_size_bounds(self, size):
        with pytest.raises(InvalidSearchRequestError):
            SearchRequest(user_id="u1", size=size)

    @pytest.mark.parametrize("year", [1899, 2101])
    def test_year_range_matches_the_column_check(self, year):
        with pytest.raises(InvalidSearchRequestError):
            SearchRequest(user_id="u1", year=year)

    def test_offset_is_derived_from_page(self):
        assert SearchRequest(user_id="u1", page=3, size=20).offset == 40

    @pytest.mark.parametrize("query", [None, "", "   ", "\n\t"])
    def test_blank_query_is_browse(self, query):
        request = SearchRequest(user_id="u1", query=query)
        assert request.is_browse is True
        assert request.normalized_query is None

    def test_query_is_stripped(self):
        assert SearchRequest(user_id="u1", query="  보안 사고  ").normalized_query == "보안 사고"

    def test_non_blank_query_is_not_browse(self):
        assert SearchRequest(user_id="u1", query="보안").is_browse is False


class TestSnippet:
    def test_short_text_is_returned_whole(self):
        assert make_snippet("짧은 본문") == "짧은 본문"

    def test_whitespace_is_collapsed(self):
        assert make_snippet("가  나\n\n다") == "가 나 다"

    def test_long_text_is_truncated(self):
        snippet = make_snippet("가" * 500)
        assert len(snippet) <= SNIPPET_MAX_CHARS + 1  # +1 for the ellipsis
        assert snippet.endswith("…")

    def test_truncation_limit_is_configurable(self):
        assert len(make_snippet("나" * 100, limit=10)) <= 11

    def test_empty_text(self):
        assert make_snippet("") == ""


class TestDocumentResult:
    def test_score_field_is_not_called_confidence(self):
        """Naming matters: cosine and trigram scores are not confidences.

        The search PoC measured cosine 0.816 for a query with no real answer,
        against 0.906 for answerable ones -- a field called `confidence` would
        invite the UI to show that as 82% certainty.
        """
        fields = set(DocumentResult.__dataclass_fields__)
        assert "retrieval_score" in fields
        assert not fields & {"confidence", "relevance", "certainty", "score"}
