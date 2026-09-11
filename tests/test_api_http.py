"""HTTP-level tests against a real, migrated PostgreSQL.

TestClient for the HTTP surface; a genuine database and (for one class) the
genuine embedding model underneath. The ACL tests matter most: a leak at this
layer is a leak to the browser.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import psycopg
import pytest

os.environ.setdefault("APP_ENV", "test")

from support import pgtest  # noqa: E402

DIMENSION = 384
DEBUG_HEADER = "X-Debug-User-Id"


class DeterministicEmbedder:
    """Keyword-per-axis embedder, so similarity is exactly scriptable."""

    AXES = {"서버": 0, "장애": 1, "출장": 2, "예산": 3,
            "인사": 4, "평가": 5, "보안": 6, "사업계획": 7}

    def vector_for(self, text: str) -> list[float]:
        axes = [i for word, i in self.AXES.items() if word in text]
        vector = [0.0] * DIMENSION
        if not axes:
            vector[DIMENSION - 1] = 1.0
            return vector
        for i in axes:
            vector[i] = 1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector]

    def embed_query_literal(self, text: str) -> str:
        return "[" + ",".join(repr(v) for v in self.vector_for(text)) + "]"


class UnavailableEmbedder:
    def embed_query_literal(self, text: str) -> str:
        from search.exceptions import SemanticSearchUnavailableError

        raise SemanticSearchUnavailableError("model missing")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for HTTP API tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def api_db(server) -> str:
    return pgtest.migrated_database(server, "api_test")


@pytest.fixture
def dsn(api_db) -> str:
    url = pgtest.psycopg_url(api_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def shared_root(tmp_path) -> Path:
    root = tmp_path / "shared"
    root.mkdir()
    return root


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def app_env(dsn, shared_root, monkeypatch):
    """Point the app's cached config/DSN at this test's database and folder."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("SHARED_ROOT", str(shared_root))

    from api import dependencies

    dependencies.get_config.cache_clear()
    dependencies.get_dsn.cache_clear()
    dependencies.get_query_embedder.cache_clear()
    yield
    dependencies.get_config.cache_clear()
    dependencies.get_dsn.cache_clear()
    dependencies.get_query_embedder.cache_clear()


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient

    from api import dependencies
    from api.app import create_app
    from ingestion.config import config_from_env
    from search.service import SearchService

    application = create_app()
    embedder = DeterministicEmbedder()

    def search_service_override():
        return SearchService(
            dependencies.connection_factory(), config_from_env(), query_embedder=embedder
        )

    application.dependency_overrides[dependencies.get_search_service] = search_service_override
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture
def corpus(conn, shared_root):
    from test_search_backend import Corpus  # reuse the search suite's builder

    return Corpus(conn, DeterministicEmbedder())


@pytest.fixture
def world(corpus, conn, shared_root):
    """Two users, two departments, four documents with different grants."""
    dept_a = corpus.department("기획조정실")
    dept_b = corpus.department("인사운영팀")
    user_a = corpus.user("user-a", dept_a)
    user_b = corpus.user("user-b", dept_b)

    doc1, rev1 = corpus.document("보안 점검 결과", department_id=dept_a, text="보안 점검", year=2026)
    doc2, _ = corpus.document("서버 장애 대응", department_id=dept_a, text="서버 장애", year=2025)
    doc3, _ = corpus.document("인사 평가 지침", department_id=dept_b, text="인사 평가")
    doc4, _ = corpus.document("극비 인사 평가 자료", department_id=dept_b, text="인사 평가")

    corpus.grant(doc1, user_id=user_a)
    corpus.grant(doc2, department_id=dept_a)
    corpus.grant(doc3, user_id=user_b)
    # doc4: no grant at all -> default deny

    # A real file for doc1 so download can be exercised.
    (shared_root / "보안 점검 결과.hwpx").write_bytes(b"ORIGINAL-BYTES")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET source_path=%s, original_filename=%s WHERE id=%s",
            ("보안 점검 결과.hwpx", "보안 점검 결과.hwpx", doc1),
        )
    return {"dept_a": dept_a, "dept_b": dept_b, "user_a": user_a, "user_b": user_b,
            "doc1": doc1, "rev1": rev1, "doc2": doc2, "doc3": doc3, "doc4": doc4}


def as_user(user_id: str) -> dict[str, str]:
    return {DEBUG_HEADER: user_id}


def item_ids(payload) -> set[str]:
    return {item["document_id"] for item in payload["items"]}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuthentication:
    def test_missing_identity_is_401(self, client, world):
        response = client.get("/api/v1/search")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_unknown_user_is_401(self, client, world):
        response = client.get(
            "/api/v1/search", headers=as_user("00000000-0000-0000-0000-000000000000")
        )
        assert response.status_code == 401

    def test_malformed_user_id_is_401_not_500(self, client, world):
        response = client.get("/api/v1/search", headers=as_user("not-a-uuid"))
        assert response.status_code == 401

    def test_debug_identity_is_refused_in_production(self, app_env, world, monkeypatch):
        """The dev identity provider must never activate in production."""
        monkeypatch.setenv("APP_ENV", "production")
        from fastapi.testclient import TestClient

        from api.app import create_app

        with TestClient(create_app()) as production_client:
            response = production_client.get(
                "/api/v1/search", headers=as_user(world["user_a"])
            )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_docs_are_disabled_in_production(self, app_env, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        from fastapi.testclient import TestClient

        from api.app import create_app

        with TestClient(create_app()) as production_client:
            assert production_client.get("/docs").status_code == 404
            assert production_client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearchHttp:
    def test_semantic_search_returns_permitted_documents(self, client, world):
        response = client.get(
            "/api/v1/search", params={"q": "서버 장애"}, headers=as_user(world["user_a"])
        )
        assert response.status_code == 200
        payload = response.json()
        assert world["doc2"] in item_ids(payload)
        assert payload["items"][0]["matched_chunk"] is not None
        assert payload["items"][0]["snippet"]

    def test_lexical_search_tolerates_a_typo(self, client, corpus, world):
        document_id, _ = corpus.document("사업계획서", text="사업예산은 3억원이다.")
        corpus.grant(document_id, user_id=world["user_a"])
        response = client.get(
            "/api/v1/search",
            params={"q": "사업예싼", "mode": "lexical"},
            headers=as_user(world["user_a"]),
        )
        assert response.status_code == 200
        assert document_id in item_ids(response.json())

    def test_browse_without_query(self, client, world):
        response = client.get("/api/v1/search", headers=as_user(world["user_a"]))
        assert response.status_code == 200
        payload = response.json()
        assert item_ids(payload) == {world["doc1"], world["doc2"]}
        for item in payload["items"]:
            assert item["snippet"] is None
            assert item["matched_chunk"] is None

    def test_browse_does_not_use_the_embedding_model(self, app_env, world):
        """Browse must work even when the model cannot be loaded."""
        from fastapi.testclient import TestClient

        from api import dependencies
        from api.app import create_app
        from ingestion.config import config_from_env
        from search.service import SearchService

        application = create_app()
        application.dependency_overrides[dependencies.get_search_service] = lambda: SearchService(
            dependencies.connection_factory(), config_from_env(),
            query_embedder=UnavailableEmbedder(),
        )
        with TestClient(application) as offline_client:
            response = offline_client.get("/api/v1/search", headers=as_user(world["user_a"]))
        assert response.status_code == 200
        assert len(response.json()["items"]) == 2

    def test_lexical_works_when_the_model_is_unavailable(self, app_env, world):
        from fastapi.testclient import TestClient

        from api import dependencies
        from api.app import create_app
        from ingestion.config import config_from_env
        from search.service import SearchService

        application = create_app()
        application.dependency_overrides[dependencies.get_search_service] = lambda: SearchService(
            dependencies.connection_factory(), config_from_env(),
            query_embedder=UnavailableEmbedder(),
        )
        with TestClient(application) as offline_client:
            lexical = offline_client.get(
                "/api/v1/search", params={"q": "보안", "mode": "lexical"},
                headers=as_user(world["user_a"]),
            )
            semantic = offline_client.get(
                "/api/v1/search", params={"q": "보안"}, headers=as_user(world["user_a"])
            )
        assert lexical.status_code == 200
        # Only the semantic route degrades.
        assert semantic.status_code == 500
        assert semantic.json()["error"]["code"] == "INTERNAL_ERROR"

    def test_response_carries_contract_fields(self, client, world):
        payload = client.get(
            "/api/v1/search", params={"q": "보안"}, headers=as_user(world["user_a"])
        ).json()
        item = payload["items"][0]
        assert {"document_id", "title", "file_type", "department", "tags",
                "updated_at", "current_revision", "has_newer_revision",
                "snippet", "matched_chunk"} <= set(item)
        assert item["file_type"] == "hwpx", "file_type stays lower case"
        assert item["matched_chunk"]["anchor"]["type"] == "paragraph"

    def test_pagination_envelope(self, client, world):
        payload = client.get(
            "/api/v1/search", params={"page": 1, "size": 1}, headers=as_user(world["user_a"])
        ).json()
        assert payload["page"] == 1 and payload["size"] == 1 and payload["total"] == 2
        assert len(payload["items"]) == 1

    def test_pagination_has_no_duplicates_or_gaps(self, client, world):
        seen = []
        for page in (1, 2):
            payload = client.get(
                "/api/v1/search", params={"page": page, "size": 1},
                headers=as_user(world["user_a"]),
            ).json()
            seen.extend(item_ids(payload))
        assert sorted(seen) == sorted({world["doc1"], world["doc2"]})


class TestSearchFilters:
    def test_department_filter(self, client, world):
        payload = client.get(
            "/api/v1/search", params={"department_id": world["dept_a"]},
            headers=as_user(world["user_a"]),
        ).json()
        assert item_ids(payload) == {world["doc1"], world["doc2"]}

    def test_department_filter_cannot_bypass_acl(self, client, world):
        payload = client.get(
            "/api/v1/search",
            params={"q": "인사 평가", "department_id": world["dept_b"]},
            headers=as_user(world["user_a"]),
        ).json()
        assert payload["items"] == [] and payload["total"] == 0

    def test_year_filter(self, client, world):
        payload = client.get(
            "/api/v1/search", params={"year": 2026}, headers=as_user(world["user_a"])
        ).json()
        assert item_ids(payload) == {world["doc1"]}

    def test_tag_filter(self, client, corpus, world):
        tag_id = corpus.tag("보안")
        corpus.attach_tag(world["doc1"], tag_id, world["user_a"])
        payload = client.get(
            "/api/v1/search", params={"tag_id": tag_id}, headers=as_user(world["user_a"])
        ).json()
        assert item_ids(payload) == {world["doc1"]}
        assert payload["items"][0]["tags"] == [{"id": tag_id, "name": "보안"}]

    def test_tag_filter_cannot_bypass_acl(self, client, corpus, world):
        tag_id = corpus.tag("공통")
        corpus.attach_tag(world["doc4"], tag_id, world["user_a"])
        payload = client.get(
            "/api/v1/search", params={"tag_id": tag_id}, headers=as_user(world["user_a"])
        ).json()
        assert payload["items"] == []

    def test_file_type_filter(self, client, world):
        assert client.get(
            "/api/v1/search", params={"file_type": "hwpx"}, headers=as_user(world["user_a"])
        ).json()["total"] == 2
        assert client.get(
            "/api/v1/search", params={"file_type": "pdf"}, headers=as_user(world["user_a"])
        ).json()["total"] == 0


class TestSearchValidation:
    def test_unknown_parameter_is_422(self, client, world):
        """A typo must not silently return unfiltered results."""
        response = client.get(
            "/api/v1/search", params={"q": "test", "yer": 2026},
            headers=as_user(world["user_a"]),
        )
        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "VALIDATION_ERROR"
        assert any(d["field"] == "yer" for d in body["details"])

    @pytest.mark.parametrize("mode", ["hybrid123", "rrf", "vector"])
    def test_invalid_mode_is_422(self, client, world, mode):
        response = client.get(
            "/api/v1/search", params={"q": "x", "mode": mode}, headers=as_user(world["user_a"])
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}, {"year": 1800}])
    def test_out_of_range_values_are_422(self, client, world, params):
        response = client.get("/api/v1/search", params=params, headers=as_user(world["user_a"]))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_overlong_query_is_422_with_its_own_code(self, client, world):
        response = client.get(
            "/api/v1/search", params={"q": "가" * 513}, headers=as_user(world["user_a"])
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "SEARCH_QUERY_TOO_LONG"

    def test_validation_error_uses_the_common_envelope(self, client, world):
        response = client.get("/api/v1/search", params={"page": 0}, headers=as_user(world["user_a"]))
        payload = response.json()
        assert set(payload) == {"error"}
        assert {"code", "message", "request_id"} <= set(payload["error"])
        assert "detail" not in payload, "FastAPI's default shape must not leak"


class TestSearchAcl:
    def test_forbidden_exact_match_is_absent_from_items_and_total(self, client, world):
        """doc4 is the best possible match for this query and has no grant.

        Semantic search applies no relevance floor, so the user's own two
        documents still come back (ranked, at similarity 0). What must never
        appear is a document the user cannot read -- and it must not be counted
        in `total` either, or the absence would be visible as a number.
        """
        payload = client.get(
            "/api/v1/search", params={"q": "인사 평가"}, headers=as_user(world["user_a"])
        ).json()
        assert world["doc4"] not in item_ids(payload)
        assert world["doc3"] not in item_ids(payload)
        assert item_ids(payload) == {world["doc1"], world["doc2"]}
        assert payload["total"] == 2, "total must count only the two permitted documents"

    def test_forbidden_document_is_absent_from_lexical_results_too(self, client, world):
        """Lexical does have a floor, so an unmatched query returns nothing."""
        payload = client.get(
            "/api/v1/search", params={"q": "인사 평가", "mode": "lexical"},
            headers=as_user(world["user_a"]),
        ).json()
        assert payload["items"] == []
        assert payload["total"] == 0

    def test_other_user_sees_their_own_document(self, client, world):
        payload = client.get(
            "/api/v1/search", params={"q": "인사 평가"}, headers=as_user(world["user_b"])
        ).json()
        assert item_ids(payload) == {world["doc3"]}

    def test_identity_cannot_be_supplied_as_a_query_parameter(self, client, world):
        response = client.get(
            "/api/v1/search",
            params={"q": "인사 평가", "user_id": world["user_b"]},
            headers=as_user(world["user_a"]),
        )
        # Not silently honoured -- it is not even an accepted parameter.
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class TestDocumentDetail:
    def test_authorized_detail(self, client, world):
        response = client.get(
            f"/api/v1/documents/{world['doc1']}", headers=as_user(world["user_a"])
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["document_id"] == world["doc1"]
        assert payload["is_searchable"] is True
        assert payload["current_revision"]["revision_no"] == 1

    def test_unauthorized_detail_is_404_not_403(self, client, world):
        response = client.get(
            f"/api/v1/documents/{world['doc4']}", headers=as_user(world["user_a"])
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"

    def test_unknown_document_is_404(self, client, world):
        response = client.get(
            "/api/v1/documents/00000000-0000-0000-0000-000000000000",
            headers=as_user(world["user_a"]),
        )
        assert response.status_code == 404

    def test_malformed_id_is_404_not_500(self, client, world):
        response = client.get("/api/v1/documents/not-a-uuid", headers=as_user(world["user_a"]))
        assert response.status_code == 404

    def test_soft_deleted_document_is_404(self, client, conn, world):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET is_deleted=TRUE, deleted_at=now() WHERE id=%s",
                (world["doc1"],),
            )
        response = client.get(
            f"/api/v1/documents/{world['doc1']}", headers=as_user(world["user_a"])
        )
        assert response.status_code == 404

    def test_detail_exposes_no_internal_fields(self, client, world):
        payload = client.get(
            f"/api/v1/documents/{world['doc1']}", headers=as_user(world["user_a"])
        ).json()
        for field in ("source_path", "original_filename", "extracted_text",
                      "parsed_structure", "content_hash"):
            assert field not in payload


class TestRevisionHistory:
    def test_authorized_history(self, client, corpus, world):
        corpus.revision(world["doc1"], 2, text="보안 점검 개정", ready=False, promote=False)
        response = client.get(
            f"/api/v1/documents/{world['doc1']}/revisions", headers=as_user(world["user_a"])
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        assert [r["revision_no"] for r in payload["items"]] == [2, 1]

    def test_is_current_follows_current_not_latest(self, client, corpus, conn, world):
        """A newer FAILED revision is latest but must not be marked current."""
        rev2 = corpus.revision(world["doc1"], 2, text="보안", ready=False, promote=False)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET embedding_status='FAILED' WHERE id=%s", (rev2,)
            )
        payload = client.get(
            f"/api/v1/documents/{world['doc1']}/revisions", headers=as_user(world["user_a"])
        ).json()
        by_no = {r["revision_no"]: r for r in payload["items"]}
        assert by_no[2]["is_current"] is False
        assert by_no[1]["is_current"] is True

    def test_unauthorized_history_is_404(self, client, world):
        response = client.get(
            f"/api/v1/documents/{world['doc4']}/revisions", headers=as_user(world["user_a"])
        )
        assert response.status_code == 404

    def test_history_exposes_no_internal_fields(self, client, world):
        item = client.get(
            f"/api/v1/documents/{world['doc1']}/revisions", headers=as_user(world["user_a"])
        ).json()["items"][0]
        for field in ("error_message", "extracted_text", "parsed_structure",
                      "parser_name", "embedding_status", "source_path_at_ingest"):
            assert field not in item


class TestDownload:
    def test_authorized_download_returns_the_original_bytes(self, client, world, shared_root):
        response = client.get(
            f"/api/v1/documents/{world['doc1']}/download", headers=as_user(world["user_a"])
        )
        assert response.status_code == 200
        assert response.content == b"ORIGINAL-BYTES"
        assert "attachment" in response.headers["content-disposition"]

    def test_download_does_not_modify_the_original(self, client, world, shared_root):
        path = shared_root / "보안 점검 결과.hwpx"
        before = path.read_bytes()
        client.get(f"/api/v1/documents/{world['doc1']}/download", headers=as_user(world["user_a"]))
        assert path.read_bytes() == before

    def test_unauthorized_download_is_404(self, client, world):
        response = client.get(
            f"/api/v1/documents/{world['doc4']}/download", headers=as_user(world["user_a"])
        )
        assert response.status_code == 404

    def test_soft_deleted_download_is_404(self, client, conn, world):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET is_deleted=TRUE, deleted_at=now() WHERE id=%s",
                (world["doc1"],),
            )
        assert client.get(
            f"/api/v1/documents/{world['doc1']}/download", headers=as_user(world["user_a"])
        ).status_code == 404

    def test_missing_source_file_is_409(self, client, world, shared_root):
        (shared_root / "보안 점검 결과.hwpx").unlink()
        response = client.get(
            f"/api/v1/documents/{world['doc1']}/download", headers=as_user(world["user_a"])
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DOCUMENT_NOT_DOWNLOADABLE"

    def test_document_without_current_revision_is_409(self, client, conn, world):
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET current_revision_id=NULL WHERE id=%s",
                        (world["doc1"],))
        response = client.get(
            f"/api/v1/documents/{world['doc1']}/download", headers=as_user(world["user_a"])
        )
        assert response.status_code == 409

    def test_path_traversal_in_stored_path_is_refused(self, client, conn, world, tmp_path):
        """A stored source_path that escapes the root must never be served."""
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"SECRET")
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET source_path=%s WHERE id=%s",
                        ("../outside.txt", world["doc1"]))
        response = client.get(
            f"/api/v1/documents/{world['doc1']}/download", headers=as_user(world["user_a"])
        )
        assert response.status_code == 409
        assert b"SECRET" not in response.content

    def test_symlink_escape_is_refused(self, client, conn, world, shared_root, tmp_path):
        outside = tmp_path / "secret.hwpx"
        outside.write_bytes(b"SECRET")
        link = shared_root / "link.hwpx"
        os.symlink(outside, link)
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET source_path='link.hwpx' WHERE id=%s",
                        (world["doc1"],))
        response = client.get(
            f"/api/v1/documents/{world['doc1']}/download", headers=as_user(world["user_a"])
        )
        assert response.status_code == 409
        assert b"SECRET" not in response.content


# ---------------------------------------------------------------------------
# Extracted-text preview
# ---------------------------------------------------------------------------

class TestTextPreview:
    """GET /documents/{id}/text.

    Serves document body text, so it is exactly as sensitive as download and
    is held to the same rules: ACL first, current READY revision only, and a
    bounded page rather than the whole document.
    """

    def preview(self, client, document_id, user_id, **params):
        return client.get(
            f"/api/v1/documents/{document_id}/text",
            params=params, headers=as_user(user_id),
        )

    def test_returns_the_extracted_text_in_document_order(self, client, corpus, world):
        doc, _ = corpus.document("점검 계획", chunks=["첫 문단", "둘째 문단", "셋째 문단"])
        corpus.grant(doc, user_id=world["user_a"])

        payload = self.preview(client, doc, world["user_a"]).json()
        assert [item["text"] for item in payload["items"]] == ["첫 문단", "둘째 문단", "셋째 문단"]
        assert [item["chunk_index"] for item in payload["items"]] == [0, 1, 2]
        assert payload["total"] == 3
        assert payload["has_more"] is False

    def test_unauthorized_preview_is_404_not_403(self, client, world):
        # Same status as detail: whether the document exists is not disclosed.
        response = self.preview(client, world["doc3"], world["user_a"])
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"

    def test_document_with_no_grant_is_refused(self, client, world):
        # doc4 has no permission row at all -- default deny still holds.
        assert self.preview(client, world["doc4"], world["user_a"]).status_code == 404

    def test_unauthenticated_preview_is_401(self, client, world):
        response = client.get(f"/api/v1/documents/{world['doc1']}/text")
        assert response.status_code == 401

    def test_body_text_is_absent_from_a_refused_response(self, client, corpus, world):
        secret = "대외비 인사 평가 본문"
        doc, _ = corpus.document("타인 문서", chunks=[secret])
        corpus.grant(doc, user_id=world["user_b"])

        response = self.preview(client, doc, world["user_a"])
        assert response.status_code == 404
        assert secret not in response.text

    def test_soft_deleted_document_is_refused(self, client, corpus, world):
        doc, _ = corpus.document("삭제된 문서", chunks=["삭제된 본문"], deleted=True)
        corpus.grant(doc, user_id=world["user_a"])
        assert self.preview(client, doc, world["user_a"]).status_code == 404

    def test_serves_the_current_revision_not_the_newest(self, client, corpus, conn, world):
        """The revision search answers from, which is not always the latest."""
        doc, current = corpus.document("개정 중 문서", chunks=["현재 검색 본문"])
        corpus.grant(doc, user_id=world["user_a"])
        # A newer revision exists but is not READY, so it is not promoted.
        corpus.revision(doc, 2, chunks=["아직 처리되지 않은 본문"], ready=False, promote=False)

        payload = self.preview(client, doc, world["user_a"]).json()
        assert payload["revision_id"] == current
        assert [item["text"] for item in payload["items"]] == ["현재 검색 본문"]
        assert "아직 처리되지 않은 본문" not in json.dumps(payload, ensure_ascii=False)

    def test_no_other_documents_text_leaks_in(self, client, corpus, world):
        doc, _ = corpus.document("내 문서", chunks=["내 본문"])
        corpus.grant(doc, user_id=world["user_a"])
        other, _ = corpus.document("남의 문서", chunks=["남의 본문"])
        corpus.grant(other, user_id=world["user_a"])

        payload = self.preview(client, doc, world["user_a"]).json()
        assert [item["text"] for item in payload["items"]] == ["내 본문"]

    def test_pages_through_a_long_document_without_gaps_or_repeats(
        self, client, corpus, world
    ):
        blocks = [f"문단 {i}" for i in range(45)]
        doc, _ = corpus.document("긴 문서", chunks=blocks)
        corpus.grant(doc, user_id=world["user_a"])

        seen: list[str] = []
        offset = 0
        while True:
            payload = self.preview(client, doc, world["user_a"], offset=offset, limit=20).json()
            assert payload["total"] == 45
            seen.extend(item["text"] for item in payload["items"])
            if not payload["has_more"]:
                break
            offset += len(payload["items"])

        assert seen == blocks

    def test_first_page_is_bounded_by_default(self, client, corpus, world):
        doc, _ = corpus.document("긴 문서", chunks=[f"문단 {i}" for i in range(80)])
        corpus.grant(doc, user_id=world["user_a"])

        payload = self.preview(client, doc, world["user_a"]).json()
        assert len(payload["items"]) == 20
        assert payload["has_more"] is True

    def test_oversized_limit_is_422_rather_than_the_whole_document(self, client, world):
        response = self.preview(client, world["doc1"], world["user_a"], limit=5000)
        assert response.status_code == 422

    def test_offset_past_the_end_is_an_empty_page_with_the_real_total(
        self, client, corpus, world
    ):
        doc, _ = corpus.document("짧은 문서", chunks=["하나", "둘"])
        corpus.grant(doc, user_id=world["user_a"])

        payload = self.preview(client, doc, world["user_a"], offset=500).json()
        assert payload["items"] == []
        assert payload["total"] == 2
        assert payload["has_more"] is False

    def test_document_with_no_current_revision_previews_nothing(
        self, client, corpus, world
    ):
        doc, _ = corpus.document("미처리 문서", chunks=["처리되지 않은 본문"],
                                 ready=False, promote=False)
        corpus.grant(doc, user_id=world["user_a"])

        payload = self.preview(client, doc, world["user_a"]).json()
        assert payload["revision_id"] is None
        assert payload["items"] == []
        assert payload["total"] == 0
        # Not-yet-ready text is text search does not serve.
        assert "처리되지 않은 본문" not in json.dumps(payload, ensure_ascii=False)

    def test_anchors_match_the_citation_format(self, client, corpus, world):
        doc, _ = corpus.document("앵커 문서", chunks=["첫 문단", "둘째 문단"])
        corpus.grant(doc, user_id=world["user_a"])

        items = self.preview(client, doc, world["user_a"]).json()["items"]
        # HWPX has no page numbers, so the anchor is a paragraph range -- the
        # same shape a chat citation carries, and no invented page.
        assert items[0]["anchor"]["type"] == "paragraph"
        assert "page" not in json.dumps(items, ensure_ascii=False)

    def test_exposes_no_internal_fields(self, client, corpus, world):
        doc, _ = corpus.document("점검 계획", chunks=["본문"])
        corpus.grant(doc, user_id=world["user_a"])

        body = self.preview(client, doc, world["user_a"]).text
        for leaked in ("source_path", "content_hash", "embedding", "chunk_id",
                       "document_revision_id", "/tmp", "shared"):
            assert leaked not in body

    def test_unknown_parameter_is_422(self, client, world):
        response = self.preview(client, world["doc1"], world["user_a"], cursor="x")
        assert response.status_code == 422

    def test_detail_response_does_not_carry_the_body(self, client, corpus, world):
        """The preview is a separate request so detail stays small."""
        body = "본문 문단 " * 200
        doc, _ = corpus.document("긴 문서", chunks=[body])
        corpus.grant(doc, user_id=world["user_a"])

        detail = client.get(
            f"/api/v1/documents/{doc}", headers=as_user(world["user_a"])
        )
        assert detail.status_code == 200
        assert body not in detail.text
        assert len(detail.content) < 4096


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_tags_endpoint(self, client, corpus, world):
        corpus.tag("보안")
        corpus.tag("인사")
        payload = client.get("/api/v1/tags", headers=as_user(world["user_a"])).json()
        assert [t["name"] for t in payload["items"]] == ["보안", "인사"]
        assert payload["total"] == 2

    def test_departments_endpoint_has_no_pagination(self, client, world):
        payload = client.get("/api/v1/departments", headers=as_user(world["user_a"])).json()
        assert set(payload) == {"items"}
        assert [d["name"] for d in payload["items"]] == ["기획조정실", "인사운영팀"]

    def test_metadata_requires_authentication(self, client):
        assert client.get("/api/v1/tags").status_code == 401
        assert client.get("/api/v1/departments").status_code == 401


# ---------------------------------------------------------------------------
# Cross-cutting security
# ---------------------------------------------------------------------------

def walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_keys(value)


class TestResponseLeaks:
    FORBIDDEN_KEYS = {"score", "retrieval_score", "similarity", "confidence",
                      "rrf_score", "distance", "relevance"}

    def test_search_response_has_no_score_key_anywhere(self, client, world):
        payload = client.get(
            "/api/v1/search", params={"q": "보안"}, headers=as_user(world["user_a"])
        ).json()
        assert set(walk_keys(payload)) & self.FORBIDDEN_KEYS == set()

    def test_no_filesystem_path_appears_in_any_response(self, client, world, shared_root):
        for url, params in [
            ("/api/v1/search", {"q": "보안"}),
            ("/api/v1/search", {}),
            (f"/api/v1/documents/{world['doc1']}", {}),
            (f"/api/v1/documents/{world['doc1']}/revisions", {}),
        ]:
            body = client.get(url, params=params, headers=as_user(world["user_a"])).text
            assert str(shared_root) not in body
            assert "source_path" not in body

    def test_error_bodies_leak_no_path(self, client, world, shared_root):
        (shared_root / "보안 점검 결과.hwpx").unlink()
        body = client.get(
            f"/api/v1/documents/{world['doc1']}/download", headers=as_user(world["user_a"])
        ).text
        assert str(shared_root) not in body

    def test_request_id_is_returned_and_matches_the_error_body(self, client, world):
        response = client.get("/api/v1/search", params={"page": 0}, headers=as_user(world["user_a"]))
        assert response.headers["X-Request-Id"]
        assert response.json()["error"]["request_id"] == response.headers["X-Request-Id"]

    def test_upstream_request_id_is_preserved(self, client, world):
        headers = as_user(world["user_a"]) | {"X-Request-Id": "upstream-123"}
        response = client.get("/api/v1/search", headers=headers)
        assert response.headers["X-Request-Id"] == "upstream-123"


class TestSqlInjectionOverHttp:
    @pytest.mark.parametrize(
        "hostile",
        ["'; DROP TABLE documents; --", "' OR '1'='1", "%' OR 1=1 --"],
    )
    def test_hostile_query_is_parameterised(self, client, conn, world, hostile):
        for mode in ("semantic", "lexical"):
            response = client.get(
                "/api/v1/search", params={"q": hostile, "mode": mode},
                headers=as_user(world["user_a"]),
            )
            assert response.status_code == 200
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents")
            assert cur.fetchone()[0] == 4


class TestNoNetwork:
    def test_search_makes_no_outbound_tcp_connection(self, client, world, monkeypatch):
        import socket

        def refuse(*args, **kwargs):
            raise AssertionError("API attempted a network connection")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        response = client.get(
            "/api/v1/search", params={"q": "보안"}, headers=as_user(world["user_a"])
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Real embedding model
# ---------------------------------------------------------------------------

def model_is_cached() -> bool:
    import glob

    for root in (os.environ.get("EMBEDDING_CACHE_DIR"), os.environ.get("HF_HOME"),
                 os.path.expanduser("~/.cache/huggingface")):
        if root and glob.glob(
            os.path.join(root, "**", "models--intfloat--multilingual-e5-small"), recursive=True
        ):
            return True
    return False


@pytest.mark.skipif(
    not model_is_cached(),
    reason="multilingual-e5-small is not in a local cache; downloads are disabled by design",
)
class TestRealModelOverHttp:
    def test_semantic_http_search_with_the_real_model(self, app_env, conn, shared_root):
        """End to end: real model, real pgvector, real HTTP."""
        from fastapi.testclient import TestClient

        from api.app import create_app
        from ingestion.config import config_from_env
        from ingestion.embedding import LocalE5Model, to_pgvector

        config = config_from_env()
        model = LocalE5Model(
            name=config.embedding_model, revision=config.embedding_model_revision,
            device=config.embedding_device, cache_dir=config.embedding_cache_dir,
            allow_download=False,
        )

        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (sso_subject) VALUES ('real') RETURNING id")
            user_id = str(cur.fetchone()[0])

        texts = {
            "서버 장애 대응 매뉴얼": "서버 장애 발생 시 비상 연락망을 통해 담당 엔지니어에게 통보한다.",
            "출장비 지급기준": "국내 출장 일비는 60,000원이며 숙박비는 실비로 정산한다.",
        }
        vectors = model.embed_passages(list(texts.values()))
        target_id = None
        for (title, body), vector in zip(texts.items(), vectors):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO documents (title, original_filename, source_path, file_type) "
                    "VALUES (%s,%s,%s,'hwpx') RETURNING id",
                    (title, f"{title}.hwpx", f"{title}.hwpx"),
                )
                document_id = str(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO document_revisions (document_id, revision_no, content_hash, "
                    "source_path_at_ingest, parse_status, parse_result_code, embedding_status, "
                    "extracted_text) VALUES (%s,1,%s,'p','SUCCESS','TEXT_EXTRACTED','SUCCESS',%s) "
                    "RETURNING id",
                    (document_id, f"h{title}", body),
                )
                revision_id = str(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO chunks (document_revision_id, chunk_index, text, "
                    "paragraph_start, paragraph_end, embedding) VALUES (%s,0,%s,0,0,%s::vector)",
                    (revision_id, body, to_pgvector(vector)),
                )
                cur.execute(
                    "UPDATE documents SET current_revision_id=%s, latest_revision_id=%s WHERE id=%s",
                    (revision_id, revision_id, document_id),
                )
                cur.execute(
                    "INSERT INTO document_permissions (document_id, user_id, permission) "
                    "VALUES (%s,%s,'READ')", (document_id, user_id),
                )
            if title.startswith("서버"):
                target_id = document_id

        with TestClient(create_app()) as real_client:
            response = real_client.get(
                "/api/v1/search",
                params={"q": "시스템이 다운되면 누구에게 연락해야 하나?"},
                headers=as_user(user_id),
            )
        assert response.status_code == 200
        assert response.json()["items"][0]["document_id"] == target_id
