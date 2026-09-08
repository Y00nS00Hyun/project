"""Parse + chunk integration tests on a real, migrated PostgreSQL.

Result codes are produced by feeding *real files* through the *real* parser --
never by stubbing a parser return value. A synthetic ENCRYPTED result would
prove the pipeline stores a string, not that the pipeline detects encryption.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ingestion.config import IngestionConfig
from ingestion.ingestion_service import IngestionService
from ingestion.sync_service import SyncService
from ingestion.tokenizers import SimpleTokenizer

from support import builders, pgtest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for ingestion DB tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def ingestion_db(server) -> str:
    return pgtest.migrated_database(server, "ingestion_parse_test")


@pytest.fixture
def dsn(ingestion_db) -> str:
    url = pgtest.psycopg_url(ingestion_db)
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
    # A small budget so chunk splitting is exercised on short fixtures.
    return IngestionConfig(shared_root=shared_root, chunk_target_tokens=16, chunk_max_tokens=16)


@pytest.fixture
def pipeline(connection_factory, config):
    """Runs sync then parse, as the `run` CLI command does."""

    def run():
        sync_result = SyncService(connection_factory, config).scan_once()
        service = IngestionService(connection_factory, config, tokenizer=SimpleTokenizer())
        parse_result = service.process_pending()
        return sync_result, parse_result

    return run


def revision_state(conn, source_path: str | None = None) -> dict:
    sql = """
        SELECT r.parse_status, r.parse_result_code, r.embedding_status, r.summary_status,
               r.tagging_status, r.is_ready, r.parser_name, r.parser_version,
               r.chunking_version, r.extracted_text, r.parsed_structure, r.id
        FROM document_revisions r JOIN documents d ON d.id = r.document_id
    """
    params = ()
    if source_path:
        sql += " WHERE d.source_path = %s"
        params = (source_path,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    keys = ("parse_status", "parse_result_code", "embedding_status", "summary_status",
            "tagging_status", "is_ready", "parser_name", "parser_version",
            "chunking_version", "extracted_text", "parsed_structure", "id")
    return dict(zip(keys, row))


def scalar(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Successful parse
# ---------------------------------------------------------------------------

class TestTextExtracted:
    def test_real_hwpx_is_parsed_and_chunked(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "2026년 사업계획서.hwpx", text="2026년 사업 예산은 3억원이다.")
        _, parse = pipeline()

        assert parse.processed == 1
        assert parse.text_extracted == 1
        assert parse.chunks_written > 0

        state = revision_state(conn)
        assert state["parse_status"] == "SUCCESS"
        assert state["parse_result_code"] == "TEXT_EXTRACTED"
        assert state["extracted_text"] and "2026년" in state["extracted_text"]

    def test_parser_provenance_is_recorded(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문입니다.")
        pipeline()
        state = revision_state(conn)
        assert state["parser_name"] == "inhouse-hwpx"
        assert state["parser_version"]
        assert state["chunking_version"] == "paragraph-v1-64t-o0"

    def test_embedding_is_left_pending_not_faked(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문입니다.")
        pipeline()
        state = revision_state(conn)
        # Embedding is a later stage. Marking it SUCCESS would make is_ready
        # true and expose a revision with no vectors to search.
        assert state["embedding_status"] == "PENDING"
        assert state["is_ready"] is False

    def test_current_revision_is_not_promoted(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문입니다.")
        pipeline()
        assert scalar(conn, "SELECT current_revision_id FROM documents") is None
        assert scalar(conn, "SELECT latest_revision_id FROM documents") is not None

    def test_job_finishes_with_the_result_code(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문입니다.")
        pipeline()
        with conn.cursor() as cur:
            cur.execute("SELECT status, result_code, finished_at FROM processing_jobs")
            status, result_code, finished_at = cur.fetchone()
        assert (status, result_code) == ("SUCCESS", "TEXT_EXTRACTED")
        assert finished_at is not None

    def test_table_structure_is_preserved_in_parsed_structure(self, pipeline, conn, shared_root):
        section = builders.MINIMAL_SECTION_XML.replace(
            "<hp:p id=\"1\" styleIDRef=\"0\"><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>",
            """<hp:p id="1" styleIDRef="0"><hp:run><hp:t>예산 현황</hp:t></hp:run></hp:p>
  <hp:p id="2" styleIDRef="0"><hp:run><hp:tbl rowCnt="2" colCnt="2">
    <hp:tr>
      <hp:tc><hp:subList><hp:p><hp:run><hp:t>부서</hp:t></hp:run></hp:p></hp:subList>
        <hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>
      <hp:tc><hp:subList><hp:p><hp:run><hp:t>예산</hp:t></hp:run></hp:p></hp:subList>
        <hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>
    </hp:tr>
    <hp:tr>
      <hp:tc><hp:subList><hp:p><hp:run><hp:t>기획실</hp:t></hp:run></hp:p></hp:subList>
        <hp:cellAddr colAddr="0" rowAddr="1"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>
      <hp:tc><hp:subList><hp:p><hp:run><hp:t>300000000</hp:t></hp:run></hp:p></hp:subList>
        <hp:cellAddr colAddr="1" rowAddr="1"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>
    </hp:tr>
  </hp:tbl></hp:run></hp:p>""",
        )
        builders.write_hwpx(shared_root / "table.hwpx", section_xml=section)
        pipeline()

        structure = revision_state(conn)["parsed_structure"]
        assert structure["tables"], "table structure must survive into parsed_structure"
        table = structure["tables"][0]
        # independent table-aware chunking is OFF, but row/column/cell/span
        # information is never discarded.
        assert table["rows"] == [["부서", "예산"], ["기획실", "300000000"]]
        assert table["cells"][0]["row_span"] == 1
        assert table["cells"][0]["column_span"] == 1

    def test_table_cells_remain_searchable_in_chunks(self, pipeline, conn, shared_root):
        section = builders.MINIMAL_SECTION_XML.replace(
            "<hp:t>{text}</hp:t>", "<hp:t>예산 현황</hp:t>"
        ).replace("</hs:sec>", """
  <hp:p id="2"><hp:run><hp:tbl rowCnt="1" colCnt="2">
    <hp:tr>
      <hp:tc><hp:subList><hp:p><hp:run><hp:t>기획실</hp:t></hp:run></hp:p></hp:subList>
        <hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>
      <hp:tc><hp:subList><hp:p><hp:run><hp:t>300000000</hp:t></hp:run></hp:p></hp:subList>
        <hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>
    </hp:tr>
  </hp:tbl></hp:run></hp:p>
</hs:sec>""")
        builders.write_hwpx(shared_root / "table.hwpx", section_xml=section)
        pipeline()
        with conn.cursor() as cur:
            cur.execute("SELECT string_agg(text, ' ') FROM chunks")
            combined = cur.fetchone()[0] or ""
        assert "기획실" in combined and "300000000" in combined


class TestChunkPersistence:
    def test_chunk_anchors_are_stored(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="첫 문단입니다.")
        pipeline()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chunk_index, paragraph_start, paragraph_end, token_count
                FROM chunks ORDER BY chunk_index
            """)
            rows = cur.fetchall()
        assert rows, "chunks must be written"
        for index, (chunk_index, start, end, tokens) in enumerate(rows):
            assert chunk_index == index, "chunk_index must be dense and ordered"
            assert start is not None and end is not None
            assert start <= end
            assert tokens is not None and tokens > 0

    def test_chunk_index_is_unique_per_revision(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text=" ".join(f"단어{i}" for i in range(80)))
        pipeline()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT document_revision_id, chunk_index, count(*)
                FROM chunks GROUP BY 1,2 HAVING count(*) > 1
            """)
            assert cur.fetchall() == []

    def test_reprocessing_rebuilds_chunks_without_leftovers(
        self, connection_factory, config, conn, shared_root
    ):
        """A re-parse must not leave stale tail chunks behind."""
        builders.write_hwpx(shared_root / "a.hwpx", text=" ".join(f"단어{i}" for i in range(80)))
        SyncService(connection_factory, config).scan_once()
        service = IngestionService(connection_factory, config, tokenizer=SimpleTokenizer())
        service.process_pending()
        first_count = scalar(conn, "SELECT count(*) FROM chunks")
        assert first_count > 1

        # Shorten the file, then force the same revision through parsing again.
        builders.write_hwpx(shared_root / "a.hwpx", text="짧은 본문")
        revision_id = scalar(conn, "SELECT id FROM document_revisions")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processing_jobs (document_revision_id, job_type, status) "
                "VALUES (%s, 'PARSE', 'PENDING')", (revision_id,)
            )
        service.process_pending()

        second_count = scalar(conn, "SELECT count(*) FROM chunks")
        assert second_count < first_count, "old chunks must be replaced, not merged"
        with conn.cursor() as cur:
            cur.execute("SELECT max(chunk_index) FROM chunks")
            assert cur.fetchone()[0] == second_count - 1


# ---------------------------------------------------------------------------
# Non-TEXT_EXTRACTED outcomes -- produced by real files through the real parser
# ---------------------------------------------------------------------------

class TestParseOutcomes:
    def _run_and_state(self, pipeline, conn):
        pipeline()
        return revision_state(conn)

    def test_empty_document(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "empty.hwpx", text="")
        state = self._run_and_state(pipeline, conn)
        assert state["parse_result_code"] == "EMPTY_DOCUMENT"
        # The parser correctly determined the document is empty; that is a
        # successful determination, not a worker failure.
        assert state["parse_status"] == "SUCCESS"

    def test_ocr_required(self, pipeline, conn, shared_root):
        builders.write_hwpx(
            shared_root / "scan.hwpx", text="",
            extra_parts={"BinData/image1.png": "not really a png"},
        )
        state = self._run_and_state(pipeline, conn)
        assert state["parse_result_code"] == "OCR_REQUIRED"
        assert state["parse_status"] == "SUCCESS"

    def test_encrypted(self, pipeline, conn, shared_root):
        builders.write_hwpx(
            shared_root / "enc.hwpx",
            extra_parts={"META-INF/manifest.xml": "<manifest><encryption-data/></manifest>"},
        )
        state = self._run_and_state(pipeline, conn)
        assert state["parse_result_code"] == "ENCRYPTED"
        assert state["parse_status"] == "SUCCESS"

    def test_corrupt(self, pipeline, conn, shared_root):
        builders.write_not_a_zip(shared_root / "broken.hwpx")
        state = self._run_and_state(pipeline, conn)
        assert state["parse_result_code"] == "CORRUPT"
        assert state["parse_status"] == "SUCCESS"

    def test_unsupported_format(self, pipeline, conn, shared_root):
        # .docx is a discoverable file_type but has no parser yet.
        (shared_root / "report.docx").write_bytes(b"PK\x03\x04 not really docx")
        state = self._run_and_state(pipeline, conn)
        assert state["parse_result_code"] == "UNSUPPORTED_FORMAT"

    def test_extension_lying_about_the_container(self, pipeline, conn, shared_root):
        """A ZIP named .hwp must not be ingested as a valid HWP."""
        builders.write_hwpx(shared_root / "actually_hwpx.hwp")
        state = self._run_and_state(pipeline, conn)
        assert state["parse_result_code"] == "UNSUPPORTED_FORMAT"
        assert scalar(conn, "SELECT count(*) FROM chunks") == 0

    @pytest.mark.parametrize(
        ("name", "builder"),
        [
            ("empty.hwpx", lambda p: builders.write_hwpx(p, text="")),
            ("broken.hwpx", builders.write_not_a_zip),
        ],
    )
    def test_non_text_outcomes_skip_downstream_and_write_no_chunks(
        self, pipeline, conn, shared_root, name, builder
    ):
        builder(shared_root / name)
        state = self._run_and_state(pipeline, conn)
        assert state["embedding_status"] == "SKIPPED"
        assert state["summary_status"] == "SKIPPED"
        assert state["tagging_status"] == "SKIPPED"
        assert state["is_ready"] is False
        assert scalar(conn, "SELECT count(*) FROM chunks") == 0
        assert state["chunking_version"] is None

    def test_result_codes_stay_inside_the_schema_domain(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "ok.hwpx", text="본문")
        builders.write_hwpx(shared_root / "empty.hwpx", text="")
        builders.write_not_a_zip(shared_root / "broken.hwpx")
        pipeline()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT parse_result_code FROM document_revisions")
            codes = {r[0] for r in cur.fetchall()}
        allowed = {
            "TEXT_EXTRACTED", "EMPTY_DOCUMENT", "OCR_REQUIRED", "ENCRYPTED",
            "CORRUPT", "UNSUPPORTED_FORMAT", "PARSE_FAILED",
        }
        assert codes <= allowed


# ---------------------------------------------------------------------------
# Isolation and recovery
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_one_unparseable_file_does_not_stop_the_batch(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "good1.hwpx", text="첫 번째 본문")
        builders.write_not_a_zip(shared_root / "bad.hwpx")
        builders.write_hwpx(shared_root / "good2.hwpx", text="두 번째 본문")

        _, parse = pipeline()
        assert parse.processed == 3
        assert parse.text_extracted == 2
        assert scalar(
            conn,
            "SELECT count(*) FROM document_revisions WHERE parse_result_code = 'TEXT_EXTRACTED'",
        ) == 2

    def test_unexpected_parser_exception_becomes_parse_failed(
        self, connection_factory, config, conn, shared_root, monkeypatch
    ):
        """A parser bug must not crash the worker."""
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        SyncService(connection_factory, config).scan_once()

        import ingestion.ingestion_service as module

        monkeypatch.setattr(
            module, "parse_document",
            lambda path: (_ for _ in ()).throw(RuntimeError("parser blew up")),
        )
        service = IngestionService(connection_factory, config, tokenizer=SimpleTokenizer())
        service.process_pending()

        state = revision_state(conn)
        assert state["parse_result_code"] == "PARSE_FAILED"
        assert state["parse_status"] == "FAILED"
        with conn.cursor() as cur:
            cur.execute("SELECT status, result_code, error_message FROM processing_jobs")
            status, code, message = cur.fetchone()
        assert (status, code) == ("FAILED", "PARSE_FAILED")
        assert "RuntimeError" in message

    def test_revision_survives_a_parse_failure(
        self, connection_factory, config, conn, shared_root, monkeypatch
    ):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        SyncService(connection_factory, config).scan_once()
        revision_id = scalar(conn, "SELECT id FROM document_revisions")

        import ingestion.ingestion_service as module
        monkeypatch.setattr(
            module, "parse_document",
            lambda path: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        IngestionService(connection_factory, config, tokenizer=SimpleTokenizer()).process_pending()

        assert scalar(conn, "SELECT id FROM document_revisions") == revision_id
        assert scalar(conn, "SELECT count(*) FROM documents") == 1

    def test_failed_job_can_be_retried(self, connection_factory, config, conn, shared_root, monkeypatch):
        builders.write_hwpx(shared_root / "a.hwpx", text="복구된 본문")
        SyncService(connection_factory, config).scan_once()

        import ingestion.ingestion_service as module
        from ingestion.repository import IngestionRepository

        monkeypatch.setattr(
            module, "parse_document",
            lambda path: (_ for _ in ()).throw(RuntimeError("transient")),
        )
        service = IngestionService(connection_factory, config, tokenizer=SimpleTokenizer())
        service.process_pending()
        assert scalar(conn, "SELECT status FROM processing_jobs") == "FAILED"

        # The transient problem clears; the job goes back on the queue.
        monkeypatch.undo()
        job_id = scalar(conn, "SELECT id FROM processing_jobs")
        with psycopg.connect(pgtest.psycopg_url(conn.info.dsn), autocommit=True) as c:
            IngestionRepository(c).reset_job_for_retry(str(job_id))

        IngestionService(connection_factory, config, tokenizer=SimpleTokenizer()).process_pending()
        state = revision_state(conn)
        assert state["parse_result_code"] == "TEXT_EXTRACTED"
        assert scalar(conn, "SELECT status FROM processing_jobs") == "SUCCESS"


class TestIdempotency:
    def test_full_pipeline_is_idempotent(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문입니다.")
        builders.write_hwpx(shared_root / "b.hwpx", text="다른 본문입니다.")
        pipeline()

        def snapshot():
            return {
                table: scalar(conn, f"SELECT count(*) FROM {table}")
                for table in ("documents", "document_revisions", "processing_jobs", "chunks")
            }

        baseline = snapshot()
        for _ in range(3):
            pipeline()
            assert snapshot() == baseline

    def test_chunk_text_is_stable_across_reruns(self, pipeline, conn, shared_root):
        builders.write_hwpx(shared_root / "a.hwpx", text="본문 내용은 동일하게 유지된다.")
        pipeline()
        with conn.cursor() as cur:
            cur.execute("SELECT chunk_index, text FROM chunks ORDER BY chunk_index")
            first = cur.fetchall()
        pipeline()
        with conn.cursor() as cur:
            cur.execute("SELECT chunk_index, text FROM chunks ORDER BY chunk_index")
            assert cur.fetchall() == first


class TestNoExternalCalls:
    def test_pipeline_makes_no_network_calls(self, pipeline, shared_root, monkeypatch):
        """Ingestion must not reach an external LLM/embedding API."""
        import socket

        def refuse(*args, **kwargs):
            raise AssertionError("ingestion attempted a network connection")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        builders.write_hwpx(shared_root / "a.hwpx", text="본문")
        # Unix-socket PostgreSQL still works; any TCP/LLM call would raise.
        pipeline()
