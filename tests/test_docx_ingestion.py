"""DOCX through the real ingestion pipeline, against a migrated PostgreSQL.

Covers what the parser tests cannot: that a DOCX becomes a document, a
revision, chunks and finally READY on the same path HWP/HWPX take, and that the
revisions already recorded as UNSUPPORTED_FORMAT can be picked up now that a
parser exists.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

os.environ.setdefault("APP_ENV", "test")

pytest.importorskip("docx", reason="python-docx is required for the DOCX parser")

import docx  # noqa: E402

from support import pgtest  # noqa: E402

from ingestion.config import IngestionConfig  # noqa: E402
from ingestion.ingestion_service import IngestionService  # noqa: E402
from ingestion.repository import IngestionRepository  # noqa: E402
from ingestion.sync_service import SyncService  # noqa: E402
from ingestion.tokenizers import SimpleTokenizer  # noqa: E402


@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for ingestion tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def docx_db(server) -> str:
    return pgtest.migrated_database(server, "docx_test")


@pytest.fixture
def dsn(docx_db) -> str:
    url = pgtest.psycopg_url(docx_db)
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
def shared_root(tmp_path) -> Path:
    root = tmp_path / "shared"
    root.mkdir()
    return root


@pytest.fixture
def config(shared_root) -> IngestionConfig:
    return IngestionConfig(shared_root=shared_root, chunk_target_tokens=32, chunk_max_tokens=32)


@pytest.fixture
def pipeline(connection_factory, config):
    def run():
        sync = SyncService(connection_factory, config).scan_once()
        parse = IngestionService(
            connection_factory, config, tokenizer=SimpleTokenizer()
        ).process_pending()
        return sync, parse

    return run


def write_docx(root: Path, name: str, build) -> Path:
    document = docx.Document()
    build(document)
    path = root / name
    document.save(str(path))
    return path


def revision(conn, source_path: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.parse_status, r.parse_result_code, r.embedding_status,
                   r.is_ready, r.parser_name, r.extracted_text, d.file_type
            FROM document_revisions r JOIN documents d ON d.id = r.document_id
            WHERE d.source_path = %s
            ORDER BY r.revision_no DESC LIMIT 1
            """,
            (source_path,),
        )
        row = cur.fetchone()
    keys = ("id", "parse_status", "parse_result_code", "embedding_status",
            "is_ready", "parser_name", "extracted_text", "file_type")
    return dict(zip(keys, row))


def counts(conn) -> dict[str, int]:
    out = {}
    with conn.cursor() as cur:
        for table in ("documents", "document_revisions", "chunks"):
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
    return out


def plan_body(d):
    d.add_heading("2026년도 정보화사업 계획", level=1)
    d.add_paragraph("본 사업은 사내 문서 검색 체계를 개선한다.")
    table = d.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "항목"
    table.rows[0].cells[1].text = "금액"
    table.rows[1].cells[0].text = "인건비"
    table.rows[1].cells[1].text = "1억 2천만원"
    d.add_paragraph("총 사업비는 3억 원이다.")


class TestDocxThroughThePipeline:
    def test_a_docx_is_parsed_chunked_and_ready_to_embed(
        self, pipeline, conn, shared_root
    ):
        write_docx(shared_root, "사업계획.docx", plan_body)
        sync, parse = pipeline()

        assert sync.new_documents == 1
        assert parse.text_extracted == 1

        state = revision(conn, "사업계획.docx")
        assert state["file_type"] == "docx"
        assert state["parse_result_code"] == "TEXT_EXTRACTED"
        assert state["parser_name"] == "inhouse-docx"
        # Same downstream hand-off as HWP/HWPX: chunks written, embedding queued.
        assert state["embedding_status"] == "PENDING"
        assert counts(conn)["chunks"] > 0

    def test_table_text_is_searchable_and_in_reading_order(
        self, pipeline, conn, shared_root
    ):
        write_docx(shared_root, "사업계획.docx", plan_body)
        pipeline()

        text = revision(conn, "사업계획.docx")["extracted_text"]
        for fragment in ("정보화사업 계획", "인건비", "1억 2천만원", "총 사업비"):
            assert fragment in text, fragment
        # The table sits between two paragraphs in the file and must sit
        # between them here too.
        assert text.index("본 사업은") < text.index("인건비") < text.index("총 사업비")

    def test_the_year_comes_from_the_shared_extraction_logic(
        self, pipeline, conn, shared_root
    ):
        # No DOCX-specific date rule: the same front-matter reader that runs
        # for HWP sees the same normalized text.
        write_docx(shared_root, "사업계획.docx", plan_body)
        pipeline()
        with conn.cursor() as cur:
            cur.execute("SELECT document_year FROM document_revisions LIMIT 1")
            assert cur.fetchone()[0] == 2026

    def test_a_broken_docx_fails_alone(self, pipeline, conn, shared_root):
        """One unreadable file must not stop the run or the other documents."""
        (shared_root / "깨진문서.docx").write_bytes(b"not a docx at all")
        write_docx(shared_root, "정상문서.docx", plan_body)

        sync, parse = pipeline()
        assert sync.new_documents == 2
        assert parse.text_extracted == 1

        broken = revision(conn, "깨진문서.docx")
        # CORRUPT, and parse_status SUCCESS: the two columns mean different
        # things. parse_status is whether the *worker* completed; the worker
        # did, and its answer was "this file is not readable". Only
        # PARSE_FAILED means the worker itself fell over. HWP and HWPX classify
        # the same garbage the same way, which is the point -- DOCX gets no
        # failure policy of its own.
        assert broken["parse_result_code"] == "CORRUPT"
        assert broken["parse_status"] == "SUCCESS"
        assert broken["embedding_status"] == "SKIPPED"
        # What actually matters: it never becomes searchable.
        assert broken["is_ready"] is False
        assert broken["extracted_text"] is None
        # The healthy one went all the way through.
        assert revision(conn, "정상문서.docx")["parse_result_code"] == "TEXT_EXTRACTED"

    def test_an_empty_docx_is_recorded_as_empty(self, pipeline, conn, shared_root):
        write_docx(shared_root, "빈문서.docx", lambda d: None)
        pipeline()
        state = revision(conn, "빈문서.docx")
        assert state["parse_result_code"] == "EMPTY_DOCUMENT"
        assert state["is_ready"] is False

    def test_the_body_is_never_written_to_the_log(
        self, pipeline, shared_root, caplog
    ):
        write_docx(shared_root, "사업계획.docx", plan_body)
        with caplog.at_level("INFO"):
            pipeline()
        logged = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
        assert "총 사업비는 3억 원이다" not in logged
        assert "1억 2천만원" not in logged


class TestReparsingUnsupportedRevisions:
    """The backfill for adding a parser to a corpus already ingested.

    An unchanged file keeps its content hash, so an ordinary scan reports it
    unchanged and never re-parses it. Without this path the revision stays at
    UNSUPPORTED_FORMAT forever even though the system can now read it.
    """

    def make_stranded(self, conn, pipeline, shared_root) -> dict:
        """A DOCX ingested, then forced back to the pre-parser state."""
        write_docx(shared_root, "사업계획.docx", plan_body)
        pipeline()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_revisions
                SET parse_status = 'SUCCESS',
                    parse_result_code = 'UNSUPPORTED_FORMAT',
                    extracted_text = NULL, parsed_structure = NULL,
                    parser_name = NULL, parser_version = NULL,
                    chunking_version = NULL,
                    embedding_status = 'SKIPPED'
                """
            )
            cur.execute("DELETE FROM chunks")
        return revision(conn, "사업계획.docx")

    def supported(self):
        from document_processing.parsers import available_parsers

        return tuple(
            extension.lstrip(".")
            for parser in available_parsers()
            for extension in getattr(parser, "extensions", ())
        )

    def requeue(self, connection_factory) -> int:
        with connection_factory() as conn:
            conn.autocommit = False
            repo = IngestionRepository(conn)
            revisions = repo.revisions_unsupported_but_now_parseable(self.supported())
            queued = sum(
                1 for revision_id in revisions
                if repo.enqueue_parse_job(revision_id) is not None
            )
            conn.commit()
        return queued

    def test_a_plain_scan_does_not_pick_it_up(
        self, pipeline, connection_factory, config, conn, shared_root
    ):
        stranded = self.make_stranded(conn, pipeline, shared_root)
        # The file has not changed, so this is the situation the CLI exists for.
        SyncService(connection_factory, config).scan_once()
        IngestionService(
            connection_factory, config, tokenizer=SimpleTokenizer()
        ).process_pending()
        assert revision(conn, "사업계획.docx")["parse_result_code"] == "UNSUPPORTED_FORMAT"
        assert stranded["id"] == revision(conn, "사업계획.docx")["id"]

    def test_requeuing_reparses_it_to_ready_without_new_rows(
        self, pipeline, connection_factory, config, conn, shared_root
    ):
        stranded = self.make_stranded(conn, pipeline, shared_root)
        before = counts(conn)

        assert self.requeue(connection_factory) == 1
        IngestionService(
            connection_factory, config, tokenizer=SimpleTokenizer()
        ).process_pending()

        after = revision(conn, "사업계획.docx")
        assert after["parse_result_code"] == "TEXT_EXTRACTED"
        assert after["parser_name"] == "inhouse-docx"
        assert after["embedding_status"] == "PENDING"
        assert "인건비" in after["extracted_text"]
        # The same revision, reused. No second document, no second revision.
        assert after["id"] == stranded["id"]
        assert counts(conn)["documents"] == before["documents"]
        assert counts(conn)["document_revisions"] == before["document_revisions"]
        assert counts(conn)["chunks"] > 0

    def test_a_healthy_hwpx_revision_is_never_requeued(
        self, pipeline, connection_factory, conn, shared_root
    ):
        """Only UNSUPPORTED_FORMAT is eligible, so working documents are safe."""
        self.make_stranded(conn, pipeline, shared_root)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET parse_result_code = 'TEXT_EXTRACTED'"
            )
        assert self.requeue(connection_factory) == 0

    def test_a_missing_file_is_not_requeued(
        self, pipeline, connection_factory, conn, shared_root
    ):
        self.make_stranded(conn, pipeline, shared_root)
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET missing_since = now()")
        assert self.requeue(connection_factory) == 0

    def test_running_it_twice_queues_nothing_extra(
        self, pipeline, connection_factory, conn, shared_root
    ):
        self.make_stranded(conn, pipeline, shared_root)
        assert self.requeue(connection_factory) == 1
        # uq_jobs_active refuses a second active PARSE job for the revision.
        assert self.requeue(connection_factory) == 0
