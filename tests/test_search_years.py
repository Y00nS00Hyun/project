"""GET /api/v1/search/years: the year filter's choices.

The list must be exactly the distinct years of documents the caller could get
back from search -- same ACL, same not-deleted, same current-READY conditions --
because a year that appears only in an unreadable document would disclose that
the document exists.
"""

from __future__ import annotations

import os

import psycopg
import pytest

os.environ.setdefault("APP_ENV", "test")

from support import pgtest  # noqa: E402

from search.repository import SearchRepository  # noqa: E402
from test_search_backend import Corpus, DeterministicEmbedder  # noqa: E402

DEBUG_HEADER = "X-Debug-User-Id"


@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for search DB tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def years_db(server) -> str:
    return pgtest.migrated_database(server, "years_test")


@pytest.fixture
def dsn(years_db) -> str:
    url = pgtest.psycopg_url(years_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def corpus(conn):
    return Corpus(conn, DeterministicEmbedder())


def years_for(dsn: str, user_id: str) -> list[int]:
    with psycopg.connect(dsn) as c:
        return SearchRepository(c).available_years(user_id)


class TestAvailableYears:
    def test_distinct_years_newest_first(self, corpus, dsn):
        reader = corpus.user("reader")
        # Two documents share 2026, so the list must collapse it to one entry.
        for index, year in enumerate((2017, 2022, 2023, 2024, 2025, 2026, 2026)):
            doc, _ = corpus.document(f"문서 {index} ({year})", year=year)
            corpus.grant(doc, user_id=reader)
        assert years_for(dsn, reader) == [2026, 2025, 2024, 2023, 2022, 2017]

    def test_a_document_without_a_year_adds_nothing(self, corpus, dsn):
        reader = corpus.user("reader")
        dated, _ = corpus.document("연도 있음", year=2024)
        undated, _ = corpus.document("연도 없음", year=None)
        corpus.grant(dated, user_id=reader)
        corpus.grant(undated, user_id=reader)
        assert years_for(dsn, reader) == [2024]

    def test_an_unreadable_documents_year_is_not_disclosed(self, corpus, dsn):
        reader = corpus.user("reader")
        other = corpus.user("other")
        mine, _ = corpus.document("내 문서", year=2024)
        theirs, _ = corpus.document("남의 문서", year=2011)
        corpus.grant(mine, user_id=reader)
        corpus.grant(theirs, user_id=other)
        # No permission row at all: default deny, and its year must not show.
        corpus.document("아무도 못 보는 문서", year=2003)

        assert years_for(dsn, reader) == [2024]
        assert years_for(dsn, other) == [2011]

    def test_a_revision_that_is_not_ready_contributes_no_year(self, corpus, dsn):
        reader = corpus.user("reader")
        never_ready, _ = corpus.document("처리 중 문서", year=2019, ready=False, promote=False)
        corpus.grant(never_ready, user_id=reader)

        # A READY current revision from 2020 with a newer, unprocessed 2018
        # revision: only the year search actually serves appears.
        served, _ = corpus.document("개정 중 문서", year=2020)
        corpus.revision(served, 2, year=2018, ready=False, promote=False)
        corpus.grant(served, user_id=reader)

        assert years_for(dsn, reader) == [2020]

    def test_a_deleted_documents_year_is_not_listed(self, corpus, dsn):
        reader = corpus.user("reader")
        gone, _ = corpus.document("삭제된 문서", year=2015, deleted=True)
        corpus.grant(gone, user_id=reader)
        assert years_for(dsn, reader) == []


class TestYearsHttp:
    @pytest.fixture
    def client(self, dsn, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setenv("SHARED_ROOT", str(tmp_path))
        from api import dependencies
        from api.app import create_app

        dependencies.get_config.cache_clear()
        dependencies.get_dsn.cache_clear()
        with TestClient(create_app()) as test_client:
            yield test_client
        dependencies.get_config.cache_clear()
        dependencies.get_dsn.cache_clear()

    def test_returns_the_years_for_the_caller(self, client, corpus):
        reader = corpus.user("reader")
        for year in (2017, 2026):
            doc, _ = corpus.document(f"문서 {year}", year=year)
            corpus.grant(doc, user_id=reader)
        response = client.get("/api/v1/search/years", headers={DEBUG_HEADER: reader})
        assert response.status_code == 200
        assert response.json() == {"years": [2026, 2017]}

    def test_requires_authentication(self, client):
        assert client.get("/api/v1/search/years").status_code == 401

    def test_rejects_filter_parameters(self, client, corpus):
        # It is not a filtered facet; accepting year= silently would imply one.
        reader = corpus.user("reader")
        response = client.get("/api/v1/search/years?year=2026", headers={DEBUG_HEADER: reader})
        assert response.status_code == 422
