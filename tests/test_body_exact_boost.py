"""The body exact-match boost, against a real migrated PostgreSQL.

The signal exists because the semantic route reads only the best chunk's cosine
and the title. A rare token living in the body -- "Zookeeper" -- had no route
into the score, and documents that never mentioned it outranked one that did.

Two conditions, and both must hold:

  1. the query occurs verbatim in the document's body
  2. that occurrence is rare across the *eligible* set

The second is what these tests are mostly about. An exact match is not by
itself evidence: a common phrase occurs verbatim nearly everywhere, and
boosting on it demotes the right answers. The threshold is a fraction of the
candidate set, and that set is ACL-filtered before anything else -- so a
document the caller cannot read can neither be boosted nor influence whether
anybody else is.
"""

from __future__ import annotations

import math

import pytest

from ingestion.config import IngestionConfig
from search.models import SearchMode, SearchRequest
from search.repository import body_exact_pattern
from search.service import SearchService

from test_search_backend import (  # noqa: F401
    DIMENSION, Corpus, DeterministicEmbedder, conn, connection_factory, corpus,
    dsn, embedder, search_db, server,
)

#: A term the embedder knows nothing about, so ranking cannot come from cosine.
RARE = "Zookeeper"


class FlatEmbedder:
    """Every document equidistant from every query.

    Removes the semantic signal entirely, which is the point: with all cosines
    equal, any ordering that appears is produced by the boosts alone. The real
    model's 0.78-0.85 band is narrow enough that this is not a caricature.
    """

    def vector_for(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSION
        vector[0] = 1.0
        return vector

    def embed_query(self, text: str) -> list[float]:
        return self.vector_for(text)

    def embed_query_literal(self, text: str) -> str:
        return "[" + ",".join(repr(v) for v in self.vector_for(text)) + "]"


@pytest.fixture
def flat_service(connection_factory, tmp_path):  # noqa: F811
    root = tmp_path / "shared"
    root.mkdir()
    return SearchService(
        connection_factory, IngestionConfig(shared_root=root), query_embedder=FlatEmbedder(),
    )


@pytest.fixture
def flat_corpus(conn):  # noqa: F811
    return Corpus(conn, FlatEmbedder())


def ranked(service, user_id, query, **kwargs) -> list[str]:
    result = service.search(SearchRequest(
        user_id=user_id, query=query, mode=SearchMode.SEMANTIC, page=1, size=50, **kwargs
    ))
    return [item.title for item in result.items]


def without_body_boost(service) -> SearchService:
    """The same service with only the body boost disabled.

    The honest control. Comparing two *different* queries would also vary the
    title trigram boost, and a difference in the result would say nothing about
    which signal produced it.
    """
    from dataclasses import replace

    return SearchService(
        service.connection_factory,
        replace(service.config, body_exact_boost_weight=0.0),
        query_embedder=FlatEmbedder(),
    )


# ---------------------------------------------------------------------------
# The pattern helper
# ---------------------------------------------------------------------------

class TestPattern:
    def test_surrounding_whitespace_is_removed_and_nothing_else(self):
        assert body_exact_pattern("  Zookeeper  ") == "Zookeeper"
        # Inner spacing, case and separators are left exactly as typed: the
        # question is whether the document contains what was written.
        assert body_exact_pattern("Apache Kafka") == "Apache Kafka"
        assert body_exact_pattern("Zoo-Keeper_2") == "Zoo-Keeper\\_2"

    def test_like_wildcards_are_escaped(self):
        # "100%" must match the characters "100%", not "100" followed by
        # anything at all.
        assert body_exact_pattern("100%") == "100\\%"
        assert body_exact_pattern("a_b") == "a\\_b"
        assert body_exact_pattern("back\\slash") == "back\\\\slash"

    def test_an_empty_query_disables_the_boost(self):
        # None, not "" -- an empty pattern would match every document.
        for value in (None, "", "   ", "\t\n"):
            assert body_exact_pattern(value) is None


# ---------------------------------------------------------------------------
# Rare term: the reported defect
# ---------------------------------------------------------------------------

class TestRareTermRanksAbove:
    @pytest.fixture
    def world(self, flat_corpus):
        user = flat_corpus.user("reader")
        containing = []
        for title in ("완료보고서_d251126", "[KISTI]SMARTer고도화-매뉴얼"):
            document, _ = flat_corpus.document(
                title, text=f"클러스터 구성에서 {RARE} 3.4.10 을 설치한다"
            )
            flat_corpus.grant(document, user_id=user)
            containing.append(title)
        for title in ("사용자매뉴얼-윤수현", "첨부 1. 제안요청서", "매뉴얼_윤수현",
                      "요구사항_정의서_윤수현", "요구사항_정의서_예시(KISTI)"):
            document, _ = flat_corpus.document(title, text="이 문서에는 해당 용어가 없다")
            flat_corpus.grant(document, user_id=user)
        return {"user": user, "containing": containing}

    def test_both_containing_documents_rank_above_every_other(self, flat_service, world):
        order = ranked(flat_service, world["user"], RARE)
        assert set(order[:2]) == set(world["containing"])
        assert len(order) == 7

    def test_no_document_is_removed(self, flat_service, world):
        # A boost, not a gate. The five documents with no match are still
        # returned, just below the two that have one.
        assert len(ranked(flat_service, world["user"], RARE)) == 7

    def test_a_case_difference_still_matches(self, flat_service, world):
        for query in ("zookeeper", "ZOOKEEPER", "ZooKeeper"):
            assert set(ranked(flat_service, world["user"], query)[:2]) == set(
                world["containing"]
            )

    def test_surrounding_whitespace_still_matches(self, flat_service, world):
        assert set(ranked(flat_service, world["user"], f"  {RARE}  ")[:2]) == set(
            world["containing"]
        )


# ---------------------------------------------------------------------------
# Common phrase: selectivity suppresses the boost
# ---------------------------------------------------------------------------

class TestCommonPhraseDoesNotBoost:
    """A phrase in most of the corpus is not evidence about which one to read."""

    @pytest.fixture
    def world(self, flat_corpus):
        user = flat_corpus.user("reader")
        # Five of seven contain the phrase -- 5/7 = 0.71, over the 0.5 ceiling.
        for index in range(5):
            document, _ = flat_corpus.document(
                f"흔한{index}", text="본문에 파일 다운로드 절차가 있다"
            )
            flat_corpus.grant(document, user_id=user)
        for index in range(2):
            document, _ = flat_corpus.document(f"없음{index}", text="관련 없는 본문")
            flat_corpus.grant(document, user_id=user)
        return {"user": user}

    @pytest.mark.parametrize("phrase", ["파일 다운로드", "본문에", "절차가 있다"])
    def test_a_widespread_phrase_changes_nothing(self, flat_service, world, phrase):
        # Same query, body boost on versus off. Identical orders mean the boost
        # did not fire -- and comparing the same query keeps the title trigram
        # boost constant, so nothing else can explain a difference.
        assert ranked(flat_service, world["user"], phrase) == ranked(
            without_body_boost(flat_service), world["user"], phrase
        )

    def test_the_ceiling_is_where_the_behaviour_changes(self, flat_service, world, conn):  # noqa: F811
        # 5 of 7 is 0.714. Raising the ceiling past it makes the same query
        # boost, which shows the suppression is the ceiling and not an accident
        # of this phrase.
        from dataclasses import replace

        permissive = SearchService(
            flat_service.connection_factory,
            replace(flat_service.config, body_exact_selectivity_max=0.8),
            query_embedder=FlatEmbedder(),
        )
        order = ranked(permissive, world["user"], "파일 다운로드")
        assert all(title.startswith("흔한") for title in order[:5])


# ---------------------------------------------------------------------------
# The denominator is the ACL-filtered eligible set
# ---------------------------------------------------------------------------

class TestSelectivityIsComputedOverEligibleDocuments:
    def test_documents_the_caller_cannot_read_do_not_count(self, flat_service, flat_corpus):
        """The hard requirement: an unreadable document influences nothing.

        Reader sees three documents, one of which contains the term: 1/3 =
        0.33, under the ceiling, so it boosts. Four more documents containing
        the same term exist and are granted to somebody else. If they were
        counted the fraction would be 5/7 = 0.71 and the boost would vanish --
        so this asserts the denominator really is per-caller.
        """
        reader = flat_corpus.user("reader")
        stranger = flat_corpus.user("stranger")

        mine, _ = flat_corpus.document("내 문서", text=f"{RARE} 설치")
        flat_corpus.grant(mine, user_id=reader)
        for index in range(2):
            other, _ = flat_corpus.document(f"내 무관 문서{index}", text="무관")
            flat_corpus.grant(other, user_id=reader)
        for index in range(4):
            hidden, _ = flat_corpus.document(f"남의 문서{index}", text=f"{RARE} 설치")
            flat_corpus.grant(hidden, user_id=stranger)

        order = ranked(flat_service, reader, RARE)
        assert len(order) == 3
        assert order[0] == "내 문서"

        # And from the other side: for the stranger the term covers 4 of 5,
        # which is over the ceiling, so nothing is boosted for them.
        stranger_order = ranked(flat_service, stranger, RARE)
        assert len(stranger_order) == 4

    def test_an_unreadable_match_cannot_suppress_a_readable_one(
        self, flat_service, flat_corpus,
    ):
        reader = flat_corpus.user("reader")
        stranger = flat_corpus.user("stranger")
        mine, _ = flat_corpus.document("내 문서", text=f"{RARE} 설치")
        flat_corpus.grant(mine, user_id=reader)
        noise, _ = flat_corpus.document("내 무관", text="무관")
        flat_corpus.grant(noise, user_id=reader)
        for index in range(20):
            hidden, _ = flat_corpus.document(f"숨은{index}", text=f"{RARE} 설치")
            flat_corpus.grant(hidden, user_id=stranger)

        assert ranked(flat_service, reader, RARE)[0] == "내 문서"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class TestWorksWithFilters:
    @pytest.fixture
    def world(self, flat_corpus):
        user = flat_corpus.user("reader")
        hit, _ = flat_corpus.document(
            "2025 보고서", text=f"{RARE} 설치", year=2025, file_type="hwpx",
        )
        flat_corpus.grant(hit, user_id=user)
        for index in range(4):
            miss, _ = flat_corpus.document(
                f"2025 기타{index}", text="무관", year=2025, file_type="hwpx",
            )
            flat_corpus.grant(miss, user_id=user)
        old, _ = flat_corpus.document(
            "2019 보고서", text=f"{RARE} 설치", year=2019, file_type="pdf",
        )
        flat_corpus.grant(old, user_id=user)
        return {"user": user}

    def test_a_year_filter_narrows_both_the_results_and_the_denominator(
        self, flat_service, world,
    ):
        order = ranked(flat_service, world["user"], RARE, year=2025)
        assert len(order) == 5
        assert order[0] == "2025 보고서"

    def test_a_file_type_filter_behaves_the_same(self, flat_service, world):
        order = ranked(flat_service, world["user"], RARE, file_type="hwpx")
        assert order[0] == "2025 보고서"

    def test_a_filter_that_leaves_only_matches_suppresses_the_boost(
        self, flat_service, world,
    ):
        # Filtering to PDFs leaves one document, which contains the term: 1/1 =
        # 1.0, over the ceiling. Nothing to reorder anyway -- what matters is
        # that the rule stays consistent with the set being searched.
        order = ranked(flat_service, world["user"], RARE, file_type="pdf")
        assert order == ["2019 보고서"]


# ---------------------------------------------------------------------------
# Nothing else moves
# ---------------------------------------------------------------------------

class TestExistingBehaviourIsUnchanged:
    @pytest.fixture
    def world(self, flat_corpus):
        user = flat_corpus.user("reader")
        for title, text in (
            ("매뉴얼_윤수현", "설치 및 실행 안내"),
            ("사용자매뉴얼-윤수현", "허밍 기반 음악 생성"),
            ("완료보고서_d251126", "시스템 구축 결과"),
        ):
            document, _ = flat_corpus.document(title, text=text)
            flat_corpus.grant(document, user_id=user)
        return {"user": user}

    @pytest.mark.parametrize("query", [
        "콧노래로 노래를 만들어주는 서비스 사용 방법",
        "검색엔진 색인 서버를 기동하는 방법",
        "온라인 베팅 사이트를 자동으로 찾아내는 시스템 구축 발주",
    ])
    def test_a_paraphrase_leaves_the_order_exactly_as_it_was(
        self, flat_service, world, query,
    ):
        # A paraphrase shares no verbatim text with its answer, so the boost
        # cannot fire and the ranking must be bit-for-bit what it was before.
        assert ranked(flat_service, world["user"], query) == ranked(
            without_body_boost(flat_service), world["user"], query
        )

    def test_a_query_answered_by_no_document_leaves_the_order_alone(
        self, flat_service, world,
    ):
        # The no-answer case: nothing contains it, so nothing is boosted and
        # whatever was offered first is still offered first.
        assert ranked(flat_service, world["user"], "김치찌개 조리법") == ranked(
            without_body_boost(flat_service), world["user"], "김치찌개 조리법"
        )

    def test_the_title_boost_still_decides_a_title_query(self, flat_service, world):
        # "수현" is in two titles and no body, so only the title boost can move
        # anything -- and it must still do so, identically.
        order = ranked(flat_service, world["user"], "수현")
        assert order[0].endswith("윤수현")
        assert order == ranked(without_body_boost(flat_service), world["user"], "수현")

    def test_the_boost_can_be_switched_off_entirely(self, flat_service, flat_corpus):
        user = flat_corpus.user("reader")
        hit, _ = flat_corpus.document("포함", text=f"{RARE} 설치")
        flat_corpus.grant(hit, user_id=user)
        for index in range(3):
            miss, _ = flat_corpus.document(f"미포함{index}", text="무관")
            flat_corpus.grant(miss, user_id=user)

        # Weight 0 restores the previous ranking exactly, so the change is
        # revertible by configuration and not only by deploying old code.
        #
        # Asserted as "the query text stops mattering" rather than "the match
        # is no longer first". With every cosine equal the leftover order is
        # the document_id tiebreak, and the match could sit first in it by
        # chance -- a test that depends on which UUIDs were generated passes or
        # fails at random.
        off = without_body_boost(flat_service)
        assert ranked(off, user, RARE) == ranked(off, user, "어디에도 없는 단어")
        assert ranked(flat_service, user, RARE)[0] == "포함"


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

class TestQueryTextIsNeverSql:
    @pytest.fixture
    def world(self, flat_corpus):
        user = flat_corpus.user("reader")
        document, _ = flat_corpus.document("정상 문서", text="100% 완료 및 a_b 처리")
        flat_corpus.grant(document, user_id=user)
        other, _ = flat_corpus.document("다른 문서", text="관련 없음")
        flat_corpus.grant(other, user_id=user)
        return {"user": user}

    @pytest.mark.parametrize("hostile", [
        "'; DROP TABLE documents; --",
        "' OR '1'='1",
        "%' OR 1=1 --",
        "\\'; DELETE FROM chunks; --",
        "%%",
        "___",
    ])
    def test_hostile_text_is_a_search_term_and_nothing_more(
        self, flat_service, world, hostile, conn,  # noqa: F811
    ):
        assert len(ranked(flat_service, world["user"], hostile)) == 2
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents")
            assert cur.fetchone()[0] == 2

    def test_a_wildcard_query_does_not_match_everything(self, flat_service, world):
        # Escaped, "%" means the literal character. One of the two documents
        # contains it, so the boost fires on that one -- what must not happen
        # is the pattern matching both and the boost covering the whole set.
        #
        # The second assertion is the one that would catch an unescaped
        # wildcard: if both matched, selectivity would be 2/2 and the boost
        # would be suppressed, leaving the ranking identical to the control.
        assert ranked(flat_service, world["user"], "%")[0] == "정상 문서"
        off = without_body_boost(flat_service)
        assert ranked(off, world["user"], "%") == ranked(off, world["user"], "없는 문자열")

    def test_a_literal_percent_matches_where_it_literally_occurs(self, flat_service, world):
        assert ranked(flat_service, world["user"], "100%")[0] == "정상 문서"

    def test_an_underscore_is_not_a_single_character_wildcard(self, flat_service, world):
        assert ranked(flat_service, world["user"], "a_b")[0] == "정상 문서"
        # "axb" would match the stored "a_b" if _ were a single-character
        # wildcard. It must not, so that query boosts nothing.
        assert ranked(flat_service, world["user"], "axb") == ranked(
            without_body_boost(flat_service), world["user"], "axb"
        )
