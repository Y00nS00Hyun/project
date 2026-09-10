"""Title-aware ranking and snippet selection.

Real PostgreSQL: both behaviours live in one SQL statement -- the boost is an
expression in ORDER BY, the snippet a LATERAL over the same rows -- and neither
is observable without running it.

The defect being pinned: the semantic route scored a document by its best body
chunk alone, so a file named "사용자매뉴얼-윤수현" had no advantage at all for
the query "수현", and the snippet came out as "(서명) |" or "Copyright © | 개정
이력" because short featureless chunks land close to anything in embedding
space.
"""

from __future__ import annotations

import psycopg
import pytest
from support import pgtest

from search.repository import (
    MIN_SNIPPET_CHARS,
    MAX_SEPARATOR_FRAGMENT_CHARS,
    MIN_SNIPPET_WORD_RATIO,
    SearchRepository,
    _substantial,
)

USER = "11111111-1111-1111-1111-111111111111"

#: (title, chunk texts). The first chunk is what a naive semantic pick would
#: land on for a name query: short, featureless, close to everything.
#: Chunk texts copied verbatim out of the verification corpus, newlines and
#: all. An earlier version of this test used hand-typed approximations and
#: passed while production still showed "Copyright © | 개정 이력" -- the real
#: string is 21 characters with a newline, the typed one was 19 with a space,
#: and they fell on opposite sides of the length threshold.
#: word_ratio 0.179 -- a representative table skeleton, not the densest one.
#: An earlier version used the *shortest* 인덱스 chunk, which happened to be
#: unusually text-heavy (0.457) and therefore genuinely substantial; the test
#: then demanded the predicate reject a chunk it had no reason to reject.
CHUNK_INDEX_TABLE = (
    "|  |  |  |  |  |  |  | \n인덱스 |  |  |  |  |  |  | 항 목 | 설 명\n"
    "@1 |  |  |  |  |  |  | 이전으로 | 직전 화면(시"
)
CHUNK_SIGNATURE = ") (서명) |"
CHUNK_COPYRIGHT = "Copyright © | \n\n개정 이력"
CHUNK_PROSE_MANUAL = "본 절에서는 허밍을 업로드해 AI 음악을 생성하는 기능에 대한 사용 설명을 제공한다."
CHUNK_PROSE_RFP = "제 안 요 청 서 사 업 명 불법 도박 사이트 탐지 웹서비스 구축 주관기관 한국과학기술원"
CHUNK_PROSE_REPORT = "통합보안정보시스템의 안정화를 위한 시스템 아키텍처를 다음 절에서 설명한다."

#: The first chunk of each document is what a naive semantic pick lands on for
#: a name query: short, featureless, close to everything in embedding space.
DOCUMENTS = [
    ("사용자매뉴얼-윤수현", [CHUNK_INDEX_TABLE, CHUNK_PROSE_MANUAL]),
    ("요구사항_정의서_윤수현", [
        "2. 기능 요구사항 - 윤수현 버전",
        "각 시스템에 저장되는 데이터에 대한 정합성 확보 방안을 기술한다.",
    ]),
    ("첨부 1. 제안요청서", [CHUNK_SIGNATURE, CHUNK_PROSE_RFP]),
    ("완료보고서_d251126", [CHUNK_COPYRIGHT, CHUNK_PROSE_REPORT]),
]


@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for these DB tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def boost_db(server) -> str:
    return pgtest.migrated_database(server, "title_boost_test")


@pytest.fixture
def dsn(boost_db) -> str:
    url = pgtest.psycopg_url(boost_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def world(conn):
    """Documents with deterministic, hand-placed embeddings.

    The vectors are fixed rather than produced by the model so the test states
    exactly what it depends on: every document is roughly equidistant from the
    query, which is the situation that made the ranking arbitrary. The proposal
    request is placed *closest*, so a title boost is the only thing that can
    move the 윤수현 documents above it.
    """
    conn.execute(
        "INSERT INTO users (id, sso_subject, is_active) VALUES (%s, 'u', TRUE)", (USER,)
    )
    dims = 384
    # Distance is driven by one coordinate; the rest is a shared constant.
    closeness = {"첨부 1. 제안요청서": 0.30, "완료보고서_d251126": 0.28,
                 "사용자매뉴얼-윤수현": 0.26, "요구사항_정의서_윤수현": 0.24}
    for title, chunks in DOCUMENTS:
        doc = conn.execute(
            "INSERT INTO documents (title, original_filename, source_path, file_type) "
            "VALUES (%s, %s, %s, 'hwp') RETURNING id",
            (title, f"{title}.hwp", f"{title}.hwp"),
        ).fetchone()[0]
        rev = conn.execute(
            "INSERT INTO document_revisions "
            "(document_id, revision_no, content_hash, source_path_at_ingest, "
            " parse_status, parse_result_code, embedding_status) "
            "VALUES (%s, 1, %s, %s, 'SUCCESS', 'TEXT_EXTRACTED', 'SUCCESS') RETURNING id",
            (doc, "a" * 64, f"{title}.hwp"),
        ).fetchone()[0]
        conn.execute("UPDATE documents SET current_revision_id=%s, latest_revision_id=%s "
                     "WHERE id=%s", (rev, rev, doc))
        conn.execute(
            "INSERT INTO document_permissions (document_id, user_id, permission) "
            "VALUES (%s, %s, 'READ')", (doc, USER)
        )
        for index, text in enumerate(chunks):
            vec = [0.0] * dims
            vec[0] = closeness[title] + index * 0.01
            vec[1] = 1.0
            conn.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text, embedding) "
                "VALUES (%s, %s, %s, %s)",
                (rev, index, text, "[" + ",".join(str(v) for v in vec) + "]"),
            )
    return conn


def search(conn, query_text, weight, query_vector=None):
    dims = 384
    vec = [0.0] * dims
    vec[0] = 0.30
    vec[1] = 1.0
    rows, _ = SearchRepository(conn).semantic_search(
        user_id=USER,
        query_vector=query_vector or "[" + ",".join(str(v) for v in vec) + "]",
        query_text=query_text,
        title_boost_weight=weight,
        department_id=None, year=None, tag_ids=(), file_type=None,
        limit=10, offset=0,
    )
    return rows


class TestTitleBoost:
    def test_without_the_boost_a_name_query_ranks_arbitrarily(self, world):
        """The bug. Nothing connects "수현" to the file names that contain it."""
        titles = [r["title"] for r in search(world, "수현", 0.0)]
        assert titles[0] == "첨부 1. 제안요청서"

    def test_with_the_boost_the_matching_titles_come_first(self, world):
        titles = [r["title"] for r in search(world, "수현", 0.075)]
        assert set(titles[:2]) == {"사용자매뉴얼-윤수현", "요구사항_정의서_윤수현"}

    def test_separators_do_not_matter(self, world):
        """"요구사항 정의서" is not a substring of "요구사항_정의서_윤수현".

        A substring test would miss this; trigram similarity does not.
        """
        titles = [r["title"] for r in search(world, "요구사항 정의서", 0.075)]
        assert titles[0] == "요구사항_정의서_윤수현"

    def test_a_query_with_no_title_match_is_left_alone(self, world):
        """Paraphrase queries must not be disturbed: the boost is 0 for them."""
        without = [r["title"] for r in search(world, "콧노래로 음악 만들기", 0.0)]
        with_boost = [r["title"] for r in search(world, "콧노래로 음악 만들기", 0.075)]
        assert without == with_boost

    def test_the_boost_never_removes_a_document(self, world):
        """Additive, not a gate: full semantic recall is preserved."""
        assert len(search(world, "수현", 0.0)) == len(search(world, "수현", 0.075)) == 4

    def test_weight_zero_is_the_old_behaviour(self, world):
        assert [r["title"] for r in search(world, "수현", 0.0)] == \
               [r["title"] for r in search(world, "", 0.075)]

    def test_browse_with_no_query_text_does_not_break(self, world):
        assert len(search(world, None, 0.075)) == 4


class TestSnippetSelection:
    def test_a_chunk_containing_the_query_wins(self, world):
        rows = {r["title"]: r["chunk_text"] for r in search(world, "윤수현", 0.075)}
        assert "윤수현" in rows["요구사항_정의서_윤수현"]

    def test_a_low_information_chunk_is_replaced(self, world):
        """"(서명) |" and "Copyright © | 개정 이력" are not explanations."""
        rows = {r["title"]: r["chunk_text"] for r in search(world, "수현", 0.075)}
        for title in ("사용자매뉴얼-윤수현", "첨부 1. 제안요청서", "완료보고서_d251126"):
            assert "인덱스 | | |" not in rows[title]
            assert "(서명)" not in rows[title]
            assert "Copyright" not in rows[title]
            assert rows[title].strip()

    def test_the_replacement_is_readable_prose(self, world):
        rows = {r["title"]: r["chunk_text"] for r in search(world, "수현", 0.075)}
        assert "허밍을 업로드해" in rows["사용자매뉴얼-윤수현"]

    def test_a_good_semantic_chunk_is_kept(self, world):
        """Case (2): no query match in the body and the semantic pick is fine.

        The lateral must return nothing and leave that chunk alone.
        """
        dims = 384
        vec = [0.0] * dims
        vec[0] = 0.25   # nearest to 요구사항_정의서_윤수현's second chunk
        vec[1] = 1.0
        rows = {
            r["title"]: r["chunk_text"]
            for r in search(world, "정합성", 0.075,
                            "[" + ",".join(str(v) for v in vec) + "]")
        }
        assert "정합성" in rows["요구사항_정의서_윤수현"]


class TestSubstantialPredicate:
    def test_thresholds_are_stated(self):
        assert MIN_SNIPPET_CHARS == 20
        assert MIN_SNIPPET_WORD_RATIO == 0.35
        assert MAX_SEPARATOR_FRAGMENT_CHARS == 40

    @pytest.mark.parametrize(
        "text,expected",
        [
            # Verbatim from the corpus -- these are the strings that actually
            # appeared as snippets, not approximations of them.
            (CHUNK_INDEX_TABLE, False),
            (CHUNK_SIGNATURE, False),
            (CHUNK_COPYRIGHT, False),
            ("짧음", False),
            ("/webapps |", False),
            (CHUNK_PROSE_MANUAL, True),
            ("Apache Storm의 Topology 상에서 Kafka의 메시지 큐를 구독하여 처리한다", True),
            # Long enough that a pipe does not condemn it: a full table row can
            # be worth quoting.
            ("SUP-34 | 양식 설명 | SMS 양식 설명을 입력한다. 발송 이력은 별도로 관리한다.", True),
        ],
    )
    def test_classifies_real_corpus_chunks(self, conn, text, expected):
        """Cases taken from the verification corpus, not invented."""
        # Named parameter: the predicate repeats the column several times and
        # a positional list would have to be kept in step with it.
        got = conn.execute(
            f"SELECT {_substantial('%(t)s')}", {"t": text}
        ).fetchone()[0]
        assert got is expected
