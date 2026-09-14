"""PDF through the real ingestion pipeline and API, against a migrated PostgreSQL.

What the parser tests cannot show: that a PDF becomes a document, a revision,
page-anchored chunks and finally READY on the path the other formats take; that
the existing `reparse-unsupported` command picks up PDFs recorded before the
parser existed; and that the API then serves page anchors and filters by type.

Embedding uses a deterministic stand-in model -- the pipeline under test is the
promotion to READY, not the vectors.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest

os.environ.setdefault("APP_ENV", "test")

pytest.importorskip("pypdf", reason="pypdf is required for the PDF parser")

from support import pgtest  # noqa: E402
from support.pdf_fixtures import write_image_only_pdf, write_text_pdf  # noqa: E402

from ingestion.config import IngestionConfig  # noqa: E402
from ingestion.embedding_service import EmbeddingService  # noqa: E402
from ingestion.ingestion_service import IngestionService  # noqa: E402
from ingestion.sync_service import SyncService  # noqa: E402
from ingestion.tokenizers import SimpleTokenizer  # noqa: E402

DEBUG_HEADER = "X-Debug-User-Id"

PLAN = [
    [["2026년도 정보화사업 계획서"],
     ["본 사업은 사내 문서 검색 체계를 개선한다."],
     ["작성일 2026. 03. 02."]],
    [["둘째 쪽 전용 문구: 인건비 1억 2천만원."],
     ["총 사업비는 3억 원이다."]],
]


class FakeModel:
    name = "fake-model"
    revision = "fake-revision"
    dimension = 384

    def embed_passages(self, texts):
        vectors = []
        for index, _ in enumerate(texts):
            vector = [0.0] * self.dimension
            vector[index % self.dimension] = 1.0
            vectors.append(vector)
        return vectors


@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for ingestion tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def pdf_db(server) -> str:
    return pgtest.migrated_database(server, "pdf_test")


@pytest.fixture
def dsn(pdf_db) -> str:
    url = pgtest.psycopg_url(pdf_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def connection_factory(dsn):
    return lambda: psycopg.connect(dsn)


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
    return IngestionConfig(shared_root=shared_root, chunk_target_tokens=64, chunk_max_tokens=64)


@pytest.fixture
def sync_and_parse(connection_factory, config):
    def run():
        sync = SyncService(connection_factory, config).scan_once()
        parse = IngestionService(
            connection_factory, config, tokenizer=SimpleTokenizer()
        ).process_pending()
        return sync, parse

    return run


@pytest.fixture
def embed(connection_factory, config):
    return lambda: EmbeddingService(connection_factory, config, model=FakeModel()).process_pending()


def revision(conn, source_path: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, d.id, r.parse_status, r.parse_result_code, r.embedding_status,
                   r.is_ready, r.parser_name, r.extracted_text, r.document_year,
                   (d.current_revision_id = r.id)
            FROM document_revisions r JOIN documents d ON d.id = r.document_id
            WHERE d.source_path = %s
            ORDER BY r.revision_no DESC LIMIT 1
            """,
            (source_path,),
        )
        row = cur.fetchone()
    keys = ("id", "document_id", "parse_status", "parse_result_code", "embedding_status",
            "is_ready", "parser_name", "extracted_text", "document_year", "is_current")
    return {k: (str(v) if k in ("id", "document_id") else v) for k, v in zip(keys, row)}


def counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        out = {}
        for table in ("documents", "document_revisions", "chunks"):
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
        return out


class TestPdfThroughThePipeline:
    def test_a_text_pdf_reaches_ready(self, sync_and_parse, embed, conn, shared_root):
        write_text_pdf(shared_root / "계획서.pdf", PLAN)
        sync, parse = sync_and_parse()
        assert sync.new_documents == 1 and parse.text_extracted == 1

        result = embed()
        assert result.succeeded == 1 and result.promoted == 1

        state = revision(conn, "계획서.pdf")
        assert state["parse_result_code"] == "TEXT_EXTRACTED"
        assert state["parser_name"] == "inhouse-pdf"
        assert state["embedding_status"] == "SUCCESS"
        assert state["is_ready"] is True and state["is_current"] is True

    def test_chunks_keep_the_page_their_text_came_from(
        self, sync_and_parse, conn, shared_root
    ):
        write_text_pdf(shared_root / "계획서.pdf", PLAN)
        sync_and_parse()
        with conn.cursor() as cur:
            cur.execute("SELECT page_number, text FROM chunks ORDER BY chunk_index")
            rows = cur.fetchall()

        assert {page for page, _ in rows} == {1, 2}
        for page, text in rows:
            if "인건비" in text or "총 사업비" in text:
                assert page == 2, text
            if "정보화사업" in text or "검색 체계" in text:
                assert page == 1, text

    def test_the_year_comes_from_the_text_like_every_format(
        self, sync_and_parse, conn, shared_root
    ):
        write_text_pdf(shared_root / "계획서.pdf", PLAN)
        sync_and_parse()
        assert revision(conn, "계획서.pdf")["document_year"] == 2026

    def test_an_image_only_pdf_is_ocr_required_and_never_ready(
        self, sync_and_parse, conn, shared_root
    ):
        write_image_only_pdf(shared_root / "스캔본.pdf", pages=2)
        sync_and_parse()
        state = revision(conn, "스캔본.pdf")
        # A determination the worker made, not a worker failure.
        assert state["parse_result_code"] == "OCR_REQUIRED"
        assert state["parse_status"] == "SUCCESS"
        assert state["embedding_status"] == "SKIPPED"
        assert state["is_ready"] is False

    def test_a_broken_pdf_fails_alone(self, sync_and_parse, conn, shared_root):
        (shared_root / "깨진문서.pdf").write_bytes(b"not a pdf at all")
        write_text_pdf(shared_root / "정상문서.pdf", PLAN)
        sync, parse = sync_and_parse()

        assert sync.new_documents == 2 and parse.text_extracted == 1
        broken = revision(conn, "깨진문서.pdf")
        assert broken["parse_result_code"] == "CORRUPT"
        assert broken["is_ready"] is False and broken["extracted_text"] is None
        assert revision(conn, "정상문서.pdf")["parse_result_code"] == "TEXT_EXTRACTED"

    def test_a_password_protected_pdf_is_encrypted(self, sync_and_parse, conn, shared_root):
        write_text_pdf(shared_root / "잠김.pdf", PLAN, user_password="secret", owner_password="o")
        sync_and_parse()
        state = revision(conn, "잠김.pdf")
        assert state["parse_result_code"] == "ENCRYPTED"
        assert state["is_ready"] is False

    def test_the_body_is_never_written_to_the_log(self, sync_and_parse, shared_root, caplog):
        write_text_pdf(shared_root / "계획서.pdf", PLAN)
        with caplog.at_level("DEBUG"):
            sync_and_parse()
        logged = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
        assert "인건비 1억 2천만원" not in logged
        assert "검색 체계를 개선한다" not in logged


class TestReparseUnsupported:
    """The existing command, unchanged, must now include PDFs."""

    def run_cli(self, dsn, shared_root, monkeypatch, capsys) -> dict:
        from ingestion import cli

        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setenv("SHARED_ROOT", str(shared_root))
        assert cli.main(["reparse-unsupported"]) == 0
        return json.loads(capsys.readouterr().out)["reparse_unsupported"]

    def strand(self, conn):
        """Force every revision back to how it looked before the parser existed."""
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE document_revisions r
                SET parse_status = 'SUCCESS', parse_result_code = 'UNSUPPORTED_FORMAT',
                    extracted_text = NULL, parsed_structure = NULL, parser_name = NULL,
                    parser_version = NULL, chunking_version = NULL,
                    embedding_status = 'SKIPPED'
                FROM documents d
                WHERE d.id = r.document_id AND d.file_type = 'pdf'
                """
            )
            cur.execute(
                "DELETE FROM chunks WHERE document_revision_id IN ("
                " SELECT r.id FROM document_revisions r JOIN documents d ON d.id = r.document_id"
                " WHERE d.file_type = 'pdf')"
            )

    def test_an_unsupported_pdf_is_reparsed_to_ready_without_new_rows(
        self, sync_and_parse, embed, connection_factory, config, conn, dsn,
        shared_root, monkeypatch, capsys,
    ):
        write_text_pdf(shared_root / "계획서.pdf", PLAN)
        sync_and_parse()
        self.strand(conn)
        before = revision(conn, "계획서.pdf")
        totals = counts(conn)

        report = self.run_cli(dsn, shared_root, monkeypatch, capsys)
        assert "pdf" in report["supported_extensions"]
        assert report["found"] == 1 and report["queued"] == 1

        IngestionService(connection_factory, config, tokenizer=SimpleTokenizer()).process_pending()
        embed()

        after = revision(conn, "계획서.pdf")
        assert after["parse_result_code"] == "TEXT_EXTRACTED"
        assert after["is_ready"] is True
        assert after["id"] == before["id"]
        assert after["document_id"] == before["document_id"]
        assert counts(conn)["documents"] == totals["documents"]
        assert counts(conn)["document_revisions"] == totals["document_revisions"]

    def test_healthy_documents_of_other_formats_are_not_requeued(
        self, sync_and_parse, conn, dsn, shared_root, monkeypatch, capsys,
    ):
        import docx

        document = docx.Document()
        document.add_paragraph("정상 DOCX 본문.")
        document.save(str(shared_root / "정상.docx"))
        write_text_pdf(shared_root / "계획서.pdf", PLAN)
        sync_and_parse()
        self.strand(conn)

        report = self.run_cli(dsn, shared_root, monkeypatch, capsys)
        # Only the stranded PDF. The DOCX parsed fine and is left alone.
        assert report["found"] == 1
        assert revision(conn, "정상.docx")["parse_result_code"] == "TEXT_EXTRACTED"


class TestApiServesPdf:
    @pytest.fixture
    def client(self, dsn, shared_root, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setenv("SHARED_ROOT", str(shared_root))
        from api import dependencies
        from api.app import create_app

        dependencies.get_config.cache_clear()
        dependencies.get_dsn.cache_clear()
        with TestClient(create_app()) as test_client:
            yield test_client
        dependencies.get_config.cache_clear()
        dependencies.get_dsn.cache_clear()

    @pytest.fixture
    def ready_pdf(self, sync_and_parse, embed, conn, shared_root) -> tuple[str, str]:
        write_text_pdf(shared_root / "계획서.pdf", PLAN)
        sync_and_parse()
        embed()
        document_id = revision(conn, "계획서.pdf")["document_id"]
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (sso_subject, name) VALUES ('reader', 'reader') RETURNING id")
            user_id = str(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO document_permissions (document_id, user_id, permission) "
                "VALUES (%s, %s, 'READ')",
                (document_id, user_id),
            )
        return document_id, user_id

    def test_text_preview_carries_real_page_anchors(self, client, ready_pdf):
        document_id, user_id = ready_pdf
        response = client.get(
            f"/api/v1/documents/{document_id}/text", headers={DEBUG_HEADER: user_id}
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert {item["anchor"]["type"] for item in items} == {"page"}
        assert {item["anchor"]["page_number"] for item in items} == {1, 2}
        page_two = [i for i in items if "인건비" in i["text"]]
        assert page_two and all(i["anchor"]["page_number"] == 2 for i in page_two)

    def test_the_file_type_filter_finds_it(self, client, ready_pdf):
        document_id, user_id = ready_pdf
        response = client.get(
            "/api/v1/search", params={"file_type": "pdf"}, headers={DEBUG_HEADER: user_id}
        )
        assert response.status_code == 200
        body = response.json()
        assert [item["document_id"] for item in body["items"]] == [document_id]
        assert body["items"][0]["file_type"] == "pdf"

    def test_download_serves_the_original_bytes(self, client, ready_pdf, shared_root):
        document_id, user_id = ready_pdf
        response = client.get(
            f"/api/v1/documents/{document_id}/download", headers={DEBUG_HEADER: user_id}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.content == (shared_root / "계획서.pdf").read_bytes()

    def test_an_unpermitted_reader_gets_404(self, client, ready_pdf, conn):
        document_id, _ = ready_pdf
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (sso_subject, name) VALUES ('other', 'other') RETURNING id")
            other = str(cur.fetchone()[0])
        response = client.get(
            f"/api/v1/documents/{document_id}/text", headers={DEBUG_HEADER: other}
        )
        assert response.status_code == 404
