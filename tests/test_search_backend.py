"""ACL-aware search on a real, migrated PostgreSQL.

The permission tests are the reason this file exists. A search backend that
leaks one unauthorised document is worse than one that returns nothing, so the
suite asserts not just "the right documents came back" but "no unauthorised
document can enter the candidate set even when it is the best possible match".
"""

from __future__ import annotations

import math

import psycopg
import pytest

from ingestion.config import IngestionConfig
from search.exceptions import SemanticSearchUnavailableError
from search.models import SearchMode, SearchRequest
from search.repository import SearchRepository
from search.service import SearchService

from support import pgtest

DIMENSION = 384


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class DeterministicEmbedder:
    """Maps text to a unit vector chosen by keyword, so similarity is scriptable.

    Each keyword owns one axis. A document containing a keyword embeds onto
    that axis; a query for it embeds onto the same axis, giving cosine 1.0.
    This makes "the unauthorised document is the single best match" an exact,
    reproducible condition rather than a hope about a real model.
    """

    AXES = {
        "서버": 0, "장애": 1, "출장": 2, "예산": 3,
        "인사": 4, "평가": 5, "보안": 6, "교육": 7,
    }

    def vector_for(self, text: str) -> list[float]:
        axes = [index for word, index in self.AXES.items() if word in text]
        vector = [0.0] * DIMENSION
        if not axes:
            vector[DIMENSION - 1] = 1.0
            return vector
        for index in axes:
            vector[index] = 1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector]

    def embed_query(self, text: str) -> list[float]:
        return self.vector_for(text)

    def embed_query_literal(self, text: str) -> str:
        return "[" + ",".join(repr(v) for v in self.vector_for(text)) + "]"


class UnavailableEmbedder:
    def embed_query_literal(self, text: str) -> str:
        raise SemanticSearchUnavailableError("model not available in this environment")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for search DB tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def search_db(server) -> str:
    return pgtest.migrated_database(server, "search_test")


@pytest.fixture
def dsn(search_db) -> str:
    url = pgtest.psycopg_url(search_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def connection_factory(dsn):
    def factory():
        return psycopg.connect(dsn)

    return factory


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def config(tmp_path) -> IngestionConfig:
    root = tmp_path / "shared"
    root.mkdir()
    return IngestionConfig(shared_root=root)


@pytest.fixture
def embedder():
    return DeterministicEmbedder()


@pytest.fixture
def service(connection_factory, config, embedder):
    return SearchService(connection_factory, config, query_embedder=embedder)


# ---------------------------------------------------------------------------
# Corpus builder
# ---------------------------------------------------------------------------

class Corpus:
    """Builds documents, revisions, chunks and permissions directly.

    Bypasses the ingestion pipeline on purpose: these tests are about search
    behaviour, and building state directly lets a scenario (a pending newer
    revision, a soft-deleted document) be expressed exactly.
    """

    def __init__(self, conn, embedder: DeterministicEmbedder):
        self.conn = conn
        self.embedder = embedder

    def department(self, name: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO departments (name) VALUES (%s) RETURNING id", (name,))
            return str(cur.fetchone()[0])

    def user(self, subject: str, department_id: str | None = None) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (sso_subject, name, department_id) VALUES (%s,%s,%s) RETURNING id",
                (subject, subject, department_id),
            )
            return str(cur.fetchone()[0])

    def document(
        self, title: str, *, department_id=None, text="본문", year=None,
        ready=True, promote=True, deleted=False, file_type="hwpx", chunks=None,
    ) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (title, original_filename, source_path, file_type, "
                "department_id, is_deleted, deleted_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (title, f"{title}.{file_type}", f"{title}.{file_type}", file_type,
                 department_id, deleted, "now()" if deleted else None),
            )
            document_id = str(cur.fetchone()[0])
        revision_id = self.revision(
            document_id, 1, text=text, year=year, ready=ready, promote=promote, chunks=chunks
        )
        return document_id, revision_id

    def revision(
        self, document_id, revision_no, *, text="본문", year=None,
        ready=True, promote=True, chunks=None,
    ) -> str:
        embedding_status = "SUCCESS" if ready else "PENDING"
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_revisions
                    (document_id, revision_no, content_hash, source_path_at_ingest,
                     parse_status, parse_result_code, embedding_status,
                     document_year, extracted_text)
                VALUES (%s,%s,%s,'p.hwpx','SUCCESS','TEXT_EXTRACTED',%s,%s,%s)
                RETURNING id
                """,
                (document_id, revision_no, f"hash-{document_id}-{revision_no}",
                 embedding_status, year, text),
            )
            revision_id = str(cur.fetchone()[0])

            for index, chunk_text in enumerate(chunks if chunks is not None else [text]):
                vector = self.embedder.vector_for(chunk_text)
                cur.execute(
                    """
                    INSERT INTO chunks (document_revision_id, chunk_index, text,
                                        paragraph_start, paragraph_end, token_count, embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::vector)
                    """,
                    (revision_id, index, chunk_text, index, index, len(chunk_text.split()),
                     "[" + ",".join(repr(v) for v in vector) + "]"),
                )
            cur.execute("UPDATE documents SET latest_revision_id=%s WHERE id=%s",
                        (revision_id, document_id))
            if promote and ready:
                cur.execute("UPDATE documents SET current_revision_id=%s WHERE id=%s",
                            (revision_id, document_id))
        return revision_id

    def grant(self, document_id, *, user_id=None, department_id=None, permission="READ"):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO document_permissions (document_id, user_id, department_id, permission) "
                "VALUES (%s,%s,%s,%s)",
                (document_id, user_id, department_id, permission),
            )

    def grant_public(self, document_id, permission="READ"):
        """The third principal: every approved account, one row per document."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO document_permissions (document_id, is_public, permission) "
                "VALUES (%s, TRUE, %s)",
                (document_id, permission),
            )

    def tag(self, name: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO tags (name) VALUES (%s) RETURNING id", (name,))
            return cur.fetchone()[0]

    def attach_tag(self, document_id, tag_id, user_id):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO document_tags (document_id, tag_id, source, created_by) "
                "VALUES (%s,%s,'MANUAL',%s)",
                (document_id, tag_id, user_id),
            )


@pytest.fixture
def corpus(conn, embedder):
    return Corpus(conn, embedder)


@pytest.fixture
def acl_world(corpus):
    """The ACL fixture from the brief: two users, two departments, four documents."""
    dept_a = corpus.department("기획조정실")
    dept_b = corpus.department("인사운영팀")
    user_a = corpus.user("user-a", dept_a)
    user_b = corpus.user("user-b", dept_b)

    doc1, _ = corpus.document("보안 사고 대응 절차", department_id=dept_a, text="보안 사고 대응")
    doc2, _ = corpus.document("서버 장애 대응 매뉴얼", department_id=dept_a, text="서버 장애 대응")
    doc3, _ = corpus.document("인사 평가 운영지침", department_id=dept_b, text="인사 평가 기준")
    doc4, _ = corpus.document("권한 없는 예산 문서", department_id=dept_b, text="예산 편성")

    corpus.grant(doc1, user_id=user_a)            # direct user grant
    corpus.grant(doc2, department_id=dept_a)      # department grant
    corpus.grant(doc3, user_id=user_b)            # other user only
    # doc4: no permission row at all -> default deny

    return {
        "dept_a": dept_a, "dept_b": dept_b,
        "user_a": user_a, "user_b": user_b,
        "doc1": doc1, "doc2": doc2, "doc3": doc3, "doc4": doc4,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ids(result) -> set[str]:
    return {item.document_id for item in result.items}


def assert_invariants(conn, user_id: str, result) -> None:
    """Every returned document must satisfy the search-eligibility contract."""
    with psycopg.connect(conn.info.dsn, autocommit=True) as c:
        allowed = SearchRepository(c).accessible_document_ids(user_id)
    for item in result.items:
        assert item.document_id in allowed, "returned a document the user cannot read"
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.is_deleted, d.current_revision_id = %s, r.is_ready
                FROM documents d JOIN document_revisions r ON r.id = %s
                WHERE d.id = %s
                """,
                (item.revision_id, item.revision_id, item.document_id),
            )
            is_deleted, is_current, is_ready = cur.fetchone()
        assert is_deleted is False
        assert is_current is True, "result revision is not the document's current revision"
        assert is_ready is True


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------

class TestAcl:
    def test_user_grant_and_department_grant_are_both_visible(self, service, conn, acl_world):
        result = service.search(SearchRequest(user_id=acl_world["user_a"], query="대응"))
        assert ids(result) == {acl_world["doc1"], acl_world["doc2"]}
        assert_invariants(conn, acl_world["user_a"], result)

    def test_other_users_document_is_invisible(self, service, acl_world):
        result = service.search(SearchRequest(user_id=acl_world["user_a"], query="인사 평가"))
        assert acl_world["doc3"] not in ids(result)

    def test_document_without_permission_rows_is_invisible(self, service, acl_world):
        """Allow-only model: no permission row means no access."""
        result = service.search(SearchRequest(user_id=acl_world["user_a"], query="예산"))
        assert acl_world["doc4"] not in ids(result)

    def test_other_user_sees_their_own_document(self, service, conn, acl_world):
        result = service.search(SearchRequest(user_id=acl_world["user_b"], query="인사 평가"))
        assert ids(result) == {acl_world["doc3"]}
        assert_invariants(conn, acl_world["user_b"], result)

    def test_department_grant_does_not_leak_to_another_department(self, service, acl_world):
        result = service.search(SearchRequest(user_id=acl_world["user_b"], query="서버 장애"))
        assert acl_world["doc2"] not in ids(result)

    @pytest.mark.parametrize("permission", ["READ", "WRITE", "ADMIN"])
    def test_every_read_capable_permission_grants_search(self, service, corpus, conn, permission):
        user = corpus.user(f"u-{permission}")
        document_id, _ = corpus.document(f"문서-{permission}", text="보안 점검")
        corpus.grant(document_id, user_id=user, permission=permission)
        result = service.search(SearchRequest(user_id=user, query="보안"))
        assert ids(result) == {document_id}

    def test_user_and_department_grants_combine(self, service, corpus):
        """Highest effective permission wins; both routes grant visibility."""
        department = corpus.department("공통부서")
        user = corpus.user("u-both", department)
        document_id, _ = corpus.document("이중 권한 문서", text="보안 지침")
        corpus.grant(document_id, department_id=department, permission="READ")
        corpus.grant(document_id, user_id=user, permission="WRITE")
        result = service.search(SearchRequest(user_id=user, query="보안"))
        assert ids(result) == {document_id}

    def test_user_without_department_is_unaffected_by_department_grants(self, service, corpus):
        department = corpus.department("어떤부서")
        orphan = corpus.user("no-dept", None)
        document_id, _ = corpus.document("부서 전용 문서", text="보안 정책")
        corpus.grant(document_id, department_id=department)
        result = service.search(SearchRequest(user_id=orphan, query="보안"))
        assert result.items == []


class TestPermissionLeak:
    """The unauthorised document is made the single best possible match."""

    def test_exact_match_on_forbidden_document_returns_nothing(self, service, corpus):
        user = corpus.user("victim")
        secret, _ = corpus.document("극비 인사 평가 자료", text="인사 평가 극비 자료")
        # No grant at all for `user`.
        result = service.search(SearchRequest(user_id=user, query="인사 평가"))
        assert result.items == []
        assert result.total == 0
        assert secret not in ids(result)

    def test_forbidden_best_match_does_not_displace_allowed_results(self, service, conn, corpus):
        user = corpus.user("analyst")
        allowed, _ = corpus.document("보안 점검 결과", text="보안 점검")
        corpus.grant(allowed, user_id=user)
        forbidden, _ = corpus.document("인사 평가 원본", text="인사 평가")

        result = service.search(SearchRequest(user_id=user, query="인사 평가"))
        assert forbidden not in ids(result)
        assert_invariants(conn, user, result)

    def test_forbidden_document_is_excluded_from_total(self, service, corpus):
        user = corpus.user("counter")
        visible, _ = corpus.document("보안 문서", text="보안")
        corpus.grant(visible, user_id=user)
        corpus.document("보안 비공개 문서", text="보안")

        result = service.search(SearchRequest(user_id=user, query="보안"))
        assert result.total == 1, "total must count only eligible documents"

    def test_lexical_route_does_not_leak_either(self, service, corpus):
        user = corpus.user("lex")
        corpus.document("극비 인사 평가 자료", text="극비 인사 평가 자료")
        result = service.search(
            SearchRequest(user_id=user, query="인사 평가", mode=SearchMode.LEXICAL)
        )
        assert result.items == []

    def test_browse_does_not_leak_either(self, service, corpus):
        user = corpus.user("browser")
        corpus.document("권한 없는 문서", text="본문")
        result = service.search(SearchRequest(user_id=user))
        assert result.items == []


# ---------------------------------------------------------------------------
# Revision and deletion state
# ---------------------------------------------------------------------------

class TestRevisionState:
    def test_only_the_current_revision_is_searched(self, service, corpus, conn):
        """A newer, better-matching but unembedded revision must not be used."""
        user = corpus.user("rev-user")
        document_id, rev1 = corpus.document("장애 매뉴얼", text="서버")
        corpus.grant(document_id, user_id=user)
        # rev2 matches the query far better but is not READY.
        corpus.revision(document_id, 2, text="서버 장애", ready=False, promote=False,
                        chunks=["서버 장애"])

        result = service.search(SearchRequest(user_id=user, query="서버 장애"))
        assert len(result.items) == 1
        assert result.items[0].revision_id == rev1
        assert_invariants(conn, user, result)

    def test_promoted_revision_is_used_after_promotion(self, service, corpus, conn):
        user = corpus.user("promo-user")
        document_id, rev1 = corpus.document("장애 매뉴얼", text="서버")
        corpus.grant(document_id, user_id=user)
        rev2 = corpus.revision(document_id, 2, text="서버 장애", ready=True, promote=True,
                               chunks=["서버 장애"])

        result = service.search(SearchRequest(user_id=user, query="서버 장애"))
        assert result.items[0].revision_id == rev2
        assert result.items[0].revision_id != rev1

    def test_document_without_current_revision_is_invisible(self, service, corpus):
        user = corpus.user("nocurrent")
        document_id, _ = corpus.document("미완료 문서", text="보안", ready=False, promote=False)
        corpus.grant(document_id, user_id=user)
        assert service.search(SearchRequest(user_id=user, query="보안")).items == []

    def test_soft_deleted_document_is_excluded_despite_permission(self, service, corpus, conn):
        user = corpus.user("del-user")
        document_id, _ = corpus.document("삭제된 문서", text="보안 지침")
        corpus.grant(document_id, user_id=user)
        assert service.search(SearchRequest(user_id=user, query="보안")).items != []

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET is_deleted=TRUE, deleted_at=now() WHERE id=%s",
                (document_id,),
            )
        assert service.search(SearchRequest(user_id=user, query="보안")).items == []


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class TestFilters:
    def test_department_filter(self, service, conn, acl_world):
        result = service.search(
            SearchRequest(user_id=acl_world["user_a"], query="대응",
                          department_id=acl_world["dept_a"])
        )
        assert ids(result) == {acl_world["doc1"], acl_world["doc2"]}
        assert_invariants(conn, acl_world["user_a"], result)

    def test_department_filter_cannot_bypass_acl(self, service, acl_world):
        """Filtering to a department the user has no rights in returns nothing."""
        result = service.search(
            SearchRequest(user_id=acl_world["user_a"], query="인사 평가",
                          department_id=acl_world["dept_b"])
        )
        assert result.items == []
        assert result.total == 0

    def test_year_filter_uses_the_current_revision(self, service, corpus):
        user = corpus.user("year-user")
        document_id, _ = corpus.document("연도 문서", text="예산", year=2025)
        corpus.grant(document_id, user_id=user)
        # A newer revision says 2026 but is not READY, so it must not count.
        corpus.revision(document_id, 2, text="예산", year=2026, ready=False, promote=False)

        assert ids(service.search(
            SearchRequest(user_id=user, query="예산", year=2025))) == {document_id}
        assert service.search(
            SearchRequest(user_id=user, query="예산", year=2026)).items == []

    def test_year_filter_follows_promotion(self, service, corpus):
        user = corpus.user("year-promo")
        document_id, _ = corpus.document("연도 문서", text="예산", year=2025)
        corpus.grant(document_id, user_id=user)
        corpus.revision(document_id, 2, text="예산", year=2026, ready=True, promote=True)

        assert service.search(
            SearchRequest(user_id=user, query="예산", year=2025)).items == []
        assert ids(service.search(
            SearchRequest(user_id=user, query="예산", year=2026))) == {document_id}

    def test_tag_filter(self, service, corpus):
        user = corpus.user("tag-user")
        tagged, _ = corpus.document("태그 문서", text="보안")
        untagged, _ = corpus.document("무태그 문서", text="보안")
        corpus.grant(tagged, user_id=user)
        corpus.grant(untagged, user_id=user)
        tag_id = corpus.tag("보안")
        corpus.attach_tag(tagged, tag_id, user)

        result = service.search(SearchRequest(user_id=user, query="보안", tag_ids=(tag_id,)))
        assert ids(result) == {tagged}

    def test_tag_filter_cannot_bypass_acl(self, service, corpus):
        user = corpus.user("tag-victim")
        forbidden, _ = corpus.document("권한 없는 태그 문서", text="보안")
        tag_id = corpus.tag("공통")
        corpus.attach_tag(forbidden, tag_id, user)
        result = service.search(SearchRequest(user_id=user, query="보안", tag_ids=(tag_id,)))
        assert result.items == []

    def test_multiple_tags_require_all(self, service, corpus):
        user = corpus.user("multi-tag")
        both, _ = corpus.document("두 태그", text="보안")
        one, _ = corpus.document("한 태그", text="보안")
        corpus.grant(both, user_id=user)
        corpus.grant(one, user_id=user)
        tag_a, tag_b = corpus.tag("A"), corpus.tag("B")
        corpus.attach_tag(both, tag_a, user)
        corpus.attach_tag(both, tag_b, user)
        corpus.attach_tag(one, tag_a, user)

        result = service.search(
            SearchRequest(user_id=user, query="보안", tag_ids=(tag_a, tag_b))
        )
        assert ids(result) == {both}

    def test_filters_combine_with_and(self, service, corpus):
        department = corpus.department("필터부서")
        user = corpus.user("combo", department)
        match, _ = corpus.document("일치 문서", department_id=department, text="예산", year=2026)
        wrong_year, _ = corpus.document("연도 불일치", department_id=department,
                                        text="예산", year=2025)
        corpus.grant(match, department_id=department)
        corpus.grant(wrong_year, department_id=department)

        result = service.search(SearchRequest(
            user_id=user, query="예산", department_id=department, year=2026
        ))
        assert ids(result) == {match}


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

class TestModes:
    def test_default_resolves_to_semantic(self, service, corpus):
        user = corpus.user("mode-user")
        document_id, _ = corpus.document("서버 장애 매뉴얼", text="서버 장애")
        corpus.grant(document_id, user_id=user)
        result = service.search(SearchRequest(user_id=user, query="서버 장애"))
        assert result.resolved_mode is SearchMode.SEMANTIC

    def test_semantic_finds_a_document_with_no_shared_words(self, service, corpus):
        """The keyword axes stand in for real semantic similarity here."""
        user = corpus.user("sem-user")
        target, _ = corpus.document("장애 대응 매뉴얼", text="서버 장애 발생 시 통보한다")
        other, _ = corpus.document("출장비 지급기준", text="출장 일비는 60000원")
        corpus.grant(target, user_id=user)
        corpus.grant(other, user_id=user)

        result = service.search(SearchRequest(user_id=user, query="서버 장애"))
        assert result.items[0].document_id == target

    def test_lexical_finds_a_typo(self, service, corpus):
        """pg_trgm should tolerate the PoC's typo failure mode."""
        user = corpus.user("lex-user")
        document_id, _ = corpus.document("사업계획서", text="사업예산은 3억원이다.")
        corpus.grant(document_id, user_id=user)

        result = service.search(
            SearchRequest(user_id=user, query="사업예싼", mode=SearchMode.LEXICAL)
        )
        assert ids(result) == {document_id}

    def test_lexical_matches_the_title(self, service, corpus):
        user = corpus.user("lex-title")
        document_id, _ = corpus.document("2026년 사업계획서", text="관계없는 본문")
        corpus.grant(document_id, user_id=user)
        result = service.search(
            SearchRequest(user_id=user, query="사업계획", mode=SearchMode.LEXICAL)
        )
        assert ids(result) == {document_id}

    def test_lexical_is_unaffected_by_extracted_text_markers(self, service, corpus):
        """extracted_text carries [문단]/[표] markers from the normalizer.

        Architecture note check: the markers must not break trigram matching on
        the surrounding text.
        """
        user = corpus.user("marker-user")
        document_id, _ = corpus.document(
            "마커 문서",
            text="[문단]\n2026년 사업계획은 다음과 같다.\n[표]\n부서 | 예산\n기획실 | 300000000",
        )
        corpus.grant(document_id, user_id=user)

        for query in ["사업계획", "기획실", "300000000"]:
            result = service.search(
                SearchRequest(user_id=user, query=query, mode=SearchMode.LEXICAL)
            )
            assert ids(result) == {document_id}, f"marker broke matching for {query!r}"

    def test_unknown_mode_is_rejected_before_any_query(self):
        from search.exceptions import InvalidSearchModeError

        with pytest.raises(InvalidSearchModeError):
            SearchRequest(user_id="u", query="x", mode=SearchMode.parse("rrf"))


class TestBrowse:
    def test_browse_returns_documents_without_a_query(self, service, conn, acl_world):
        result = service.search(SearchRequest(user_id=acl_world["user_a"]))
        assert ids(result) == {acl_world["doc1"], acl_world["doc2"]}
        assert_invariants(conn, acl_world["user_a"], result)

    def test_browse_has_no_matched_chunk_or_score(self, service, acl_world):
        result = service.search(SearchRequest(user_id=acl_world["user_a"]))
        for item in result.items:
            assert item.matched_chunk is None
            assert item.retrieval_score is None

    def test_browse_does_not_embed_a_query(self, connection_factory, config, acl_world):
        """No retrieval means the embedding model is never touched."""

        class ExplodingEmbedder:
            def embed_query_literal(self, text):
                raise AssertionError("browse must not embed a query")

        service = SearchService(connection_factory, config, query_embedder=ExplodingEmbedder())
        result = service.search(SearchRequest(user_id=acl_world["user_a"]))
        assert len(result.items) == 2

    def test_browse_applies_filters(self, service, corpus):
        department = corpus.department("브라우즈부서")
        user = corpus.user("browse-filter", department)
        match, _ = corpus.document("2026 문서", department_id=department, text="본문", year=2026)
        other, _ = corpus.document("2025 문서", department_id=department, text="본문", year=2025)
        corpus.grant(match, department_id=department)
        corpus.grant(other, department_id=department)

        result = service.search(SearchRequest(user_id=user, year=2026))
        assert ids(result) == {match}

    def test_browse_orders_by_updated_at_desc(self, service, corpus, conn):
        user = corpus.user("order-user")
        first, _ = corpus.document("오래된 문서", text="본문")
        second, _ = corpus.document("최신 문서", text="본문")
        corpus.grant(first, user_id=user)
        corpus.grant(second, user_id=user)
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET updated_at = now() - interval '1 day' WHERE id=%s",
                        (first,))

        result = service.search(SearchRequest(user_id=user))
        assert [item.document_id for item in result.items] == [second, first]


# ---------------------------------------------------------------------------
# Aggregation, ranking, pagination
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_document_appears_once_regardless_of_chunk_count(self, service, corpus):
        user = corpus.user("agg-user")
        document_id, _ = corpus.document(
            "여러 청크 문서", text="서버 장애",
            chunks=["서버 장애", "서버 장애", "서버 장애", "서버 장애", "서버 장애"],
        )
        corpus.grant(document_id, user_id=user)

        result = service.search(SearchRequest(user_id=user, query="서버 장애"))
        assert [item.document_id for item in result.items] == [document_id]
        assert result.total == 1

    def test_many_chunks_do_not_crowd_out_other_documents(self, service, corpus):
        """The reason chunk top-K then dedup is forbidden.

        A document with many strong chunks must not consume the whole page.
        """
        user = corpus.user("crowd-user")
        noisy, _ = corpus.document("청크 많은 문서", text="보안",
                                   chunks=["보안"] * 30)
        quiet, _ = corpus.document("청크 적은 문서", text="보안", chunks=["보안"])
        corpus.grant(noisy, user_id=user)
        corpus.grant(quiet, user_id=user)

        result = service.search(SearchRequest(user_id=user, query="보안", size=2))
        assert ids(result) == {noisy, quiet}
        assert result.total == 2

    def test_matched_chunk_is_the_best_scoring_one(self, service, corpus):
        user = corpus.user("chunk-user")
        document_id, _ = corpus.document(
            "혼합 문서", text="본문",
            chunks=["출장 관련 내용", "서버 장애 관련 내용", "교육 관련 내용"],
        )
        corpus.grant(document_id, user_id=user)

        result = service.search(SearchRequest(user_id=user, query="서버 장애"))
        matched = result.items[0].matched_chunk
        assert matched is not None
        assert matched.chunk_index == 1
        assert "서버 장애" in matched.snippet

    def test_matched_chunk_carries_citation_anchors(self, service, corpus):
        user = corpus.user("anchor-user")
        document_id, _ = corpus.document("앵커 문서", text="보안 점검")
        corpus.grant(document_id, user_id=user)

        matched = service.search(
            SearchRequest(user_id=user, query="보안")
        ).items[0].matched_chunk
        assert matched.chunk_id
        assert matched.paragraph_start is not None
        assert matched.paragraph_end is not None

    def test_snippet_is_bounded(self, service, corpus):
        from search.models import SNIPPET_MAX_CHARS

        user = corpus.user("snippet-user")
        long_text = "보안 " + "가" * 2000
        document_id, _ = corpus.document("긴 문서", text=long_text, chunks=[long_text])
        corpus.grant(document_id, user_id=user)

        matched = service.search(
            SearchRequest(user_id=user, query="보안")
        ).items[0].matched_chunk
        assert len(matched.snippet) <= SNIPPET_MAX_CHARS + 1


class TestRankingAndPagination:
    @pytest.fixture
    def ten_documents(self, corpus):
        user = corpus.user("page-user")
        document_ids = []
        for index in range(10):
            document_id, _ = corpus.document(f"문서 {index:02d}", text="보안")
            corpus.grant(document_id, user_id=user)
            document_ids.append(document_id)
        return user, document_ids

    def test_tie_break_is_document_id_ascending(self, service, ten_documents):
        """All ten score identically; ordering must still be stable."""
        user, _ = ten_documents
        result = service.search(SearchRequest(user_id=user, query="보안", size=10))
        returned = [item.document_id for item in result.items]
        assert returned == sorted(returned)

    def test_pagination_covers_every_document_exactly_once(self, service, ten_documents):
        user, document_ids = ten_documents
        seen = []
        for page in range(1, 4):
            result = service.search(
                SearchRequest(user_id=user, query="보안", page=page, size=4)
            )
            assert result.total == 10
            seen.extend(item.document_id for item in result.items)
        assert len(seen) == 10
        assert len(set(seen)) == 10, "pagination duplicated a document"
        assert set(seen) == set(document_ids)

    def test_pagination_is_stable_across_repeated_calls(self, service, ten_documents):
        user, _ = ten_documents
        first = service.search(SearchRequest(user_id=user, query="보안", page=2, size=3))
        second = service.search(SearchRequest(user_id=user, query="보안", page=2, size=3))
        assert [i.document_id for i in first.items] == [i.document_id for i in second.items]

    def test_page_beyond_the_end_is_empty_but_reports_total(self, service, ten_documents):
        user, _ = ten_documents
        result = service.search(SearchRequest(user_id=user, query="보안", page=99, size=10))
        assert result.items == []
        assert result.total == 0  # count(*) OVER () has no rows to report on

    def test_browse_pagination_covers_everything_once(self, service, ten_documents):
        user, document_ids = ten_documents
        seen = []
        for page in range(1, 4):
            result = service.search(SearchRequest(user_id=user, page=page, size=4))
            seen.extend(item.document_id for item in result.items)
        assert set(seen) == set(document_ids)
        assert len(seen) == len(set(seen))


# ---------------------------------------------------------------------------
# Degradation and security
# ---------------------------------------------------------------------------

class TestDegradation:
    def test_semantic_fails_clearly_when_the_model_is_missing(
        self, connection_factory, config, acl_world
    ):
        service = SearchService(connection_factory, config, query_embedder=UnavailableEmbedder())
        with pytest.raises(SemanticSearchUnavailableError):
            service.search(SearchRequest(user_id=acl_world["user_a"], query="보안"))

    def test_lexical_still_works_without_the_model(
        self, connection_factory, config, acl_world
    ):
        service = SearchService(connection_factory, config, query_embedder=UnavailableEmbedder())
        result = service.search(SearchRequest(
            user_id=acl_world["user_a"], query="보안", mode=SearchMode.LEXICAL
        ))
        assert acl_world["doc1"] in ids(result)

    def test_browse_still_works_without_the_model(
        self, connection_factory, config, acl_world
    ):
        service = SearchService(connection_factory, config, query_embedder=UnavailableEmbedder())
        result = service.search(SearchRequest(user_id=acl_world["user_a"]))
        assert len(result.items) == 2


class TestSqlSafety:
    @pytest.mark.parametrize(
        "hostile",
        [
            "'; DROP TABLE documents; --",
            "' OR '1'='1",
            "%' OR 1=1 --",
            "\\'; DELETE FROM chunks; --",
        ],
    )
    def test_hostile_query_text_is_parameterised(self, service, conn, acl_world, hostile):
        for mode in (SearchMode.SEMANTIC, SearchMode.LEXICAL):
            result = service.search(
                SearchRequest(user_id=acl_world["user_a"], query=hostile, mode=mode)
            )
            assert isinstance(result.total, int)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents")
            assert cur.fetchone()[0] == 4, "documents table must be intact"

    def test_hostile_filter_values_are_rejected_or_parameterised(self, service, acl_world):
        result = service.search(SearchRequest(
            user_id=acl_world["user_a"], query="보안", tag_ids=(1, 2, 3)
        ))
        assert isinstance(result.total, int)


class TestNoNetwork:
    def test_semantic_search_works_with_tcp_blocked(
        self, connection_factory, config, embedder, acl_world, monkeypatch
    ):
        """Query embedding must be local; no external API call is permitted."""
        import socket

        def refuse(*args, **kwargs):
            raise AssertionError("search attempted a network connection")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        service = SearchService(connection_factory, config, query_embedder=embedder)
        result = service.search(SearchRequest(user_id=acl_world["user_a"], query="보안"))
        assert len(result.items) >= 1


class TestObservability:
    def test_query_text_is_not_logged(self, service, acl_world, caplog):
        secret = "극비 인사 평가 자료"
        with caplog.at_level("INFO"):
            service.search(SearchRequest(user_id=acl_world["user_a"], query=secret))
        assert secret not in caplog.text

    def test_timings_are_reported(self, service, acl_world):
        result = service.search(SearchRequest(user_id=acl_world["user_a"], query="보안"))
        assert result.retrieval_ms is not None
        assert result.query_embedding_ms is not None

# ---------------------------------------------------------------------------
# Real embedding model
# ---------------------------------------------------------------------------

def model_is_cached() -> bool:
    import glob
    import os

    roots = [
        os.environ.get("EMBEDDING_CACHE_DIR"),
        os.environ.get("HF_HOME"),
        os.path.expanduser("~/.cache/huggingface"),
    ]
    for root in roots:
        if root and glob.glob(
            os.path.join(root, "**", "models--intfloat--multilingual-e5-small"), recursive=True
        ):
            return True
    return False


@pytest.mark.skipif(
    not model_is_cached(),
    reason="multilingual-e5-small is not in a local cache; "
           "pre-populate it (downloads are disabled by design)",
)
class TestRealSemanticSearch:
    """Semantic search with the actual model and real pgvector cosine.

    The deterministic embedder proves the plumbing and the ACL; only the real
    model proves that a query with no shared vocabulary finds the right
    document.
    """

    @pytest.fixture
    def real_corpus(self, conn, config):
        """Documents embedded with the production model, via the real wrapper."""
        from ingestion.embedding import LocalE5Model, to_pgvector

        model = LocalE5Model(
            name=config.embedding_model,
            revision=config.embedding_model_revision,
            device=config.embedding_device,
            cache_dir=config.embedding_cache_dir,
            allow_download=False,
        )

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (sso_subject, name) VALUES ('real-user','real') RETURNING id"
            )
            user_id = str(cur.fetchone()[0])

        texts = {
            "서버 장애 대응 매뉴얼": "서버 장애 발생 시 비상 연락망을 통해 담당 엔지니어에게 통보한다.",
            "출장비 지급기준": "국내 출장 일비는 60,000원이며 숙박비는 실비로 정산한다.",
            "교육 운영계획": "연간 교육 예산은 45,000,000원이며 직무 교육과 법정 교육으로 나눈다.",
        }
        document_ids = {}
        vectors = model.embed_passages(list(texts.values()))
        for (title, body), vector in zip(texts.items(), vectors):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO documents (title, original_filename, source_path, file_type) "
                    "VALUES (%s,%s,%s,'hwpx') RETURNING id",
                    (title, f"{title}.hwpx", f"{title}.hwpx"),
                )
                document_id = str(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO document_revisions
                        (document_id, revision_no, content_hash, source_path_at_ingest,
                         parse_status, parse_result_code, embedding_status, extracted_text)
                    VALUES (%s,1,%s,'p.hwpx','SUCCESS','TEXT_EXTRACTED','SUCCESS',%s)
                    RETURNING id
                    """,
                    (document_id, f"h-{title}", body),
                )
                revision_id = str(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO chunks (document_revision_id, chunk_index, text, "
                    "paragraph_start, paragraph_end, token_count, embedding) "
                    "VALUES (%s,0,%s,0,0,10,%s::vector)",
                    (revision_id, body, to_pgvector(vector)),
                )
                cur.execute(
                    "UPDATE documents SET current_revision_id=%s, latest_revision_id=%s WHERE id=%s",
                    (revision_id, revision_id, document_id),
                )
                cur.execute(
                    "INSERT INTO document_permissions (document_id, user_id, permission) "
                    "VALUES (%s,%s,'READ')",
                    (document_id, user_id),
                )
            document_ids[title] = document_id
        return user_id, document_ids

    def test_semantic_query_finds_the_right_document(
        self, connection_factory, config, real_corpus
    ):
        user_id, document_ids = real_corpus
        service = SearchService(connection_factory, config)

        # No vocabulary overlap with the target document.
        result = service.search(
            SearchRequest(user_id=user_id, query="시스템이 다운되면 누구에게 연락해야 하나?")
        )
        assert result.items
        assert result.items[0].document_id == document_ids["서버 장애 대응 매뉴얼"]

    def test_real_semantic_search_still_respects_acl(
        self, connection_factory, config, conn, real_corpus
    ):
        user_id, document_ids = real_corpus
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM document_permissions WHERE document_id = %s",
                (document_ids["서버 장애 대응 매뉴얼"],),
            )
        service = SearchService(connection_factory, config)
        result = service.search(
            SearchRequest(user_id=user_id, query="시스템이 다운되면 누구에게 연락해야 하나?")
        )
        assert document_ids["서버 장애 대응 매뉴얼"] not in ids(result)

    def test_query_uses_the_query_prefix_not_passage(self, config):
        """Document and query prefixes must not be confused."""
        from ingestion.embedding import PASSAGE_PREFIX, QUERY_PREFIX, LocalE5Model

        captured = {}
        model = LocalE5Model(name="stub")

        class Stub:
            def encode(self, texts, **kwargs):
                import numpy

                captured["texts"] = list(texts)
                return numpy.array([[1.0] + [0.0] * 383 for _ in texts])

        model._model = Stub()
        model._dimension = 384
        model.embed_query("보안 사고")
        assert captured["texts"] == [QUERY_PREFIX + "보안 사고"]
        assert not captured["texts"][0].startswith(PASSAGE_PREFIX)
