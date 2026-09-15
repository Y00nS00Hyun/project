"""Administrator rename / move of an original file, on a real filesystem and DB.

Every test uses files created under a pytest tmp directory. No production
document is touched. The read root and the write root are the same directory
here -- in deployment they are two mounts of one host folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest

os.environ.setdefault("APP_ENV", "test")

from support import pgtest  # noqa: E402

from ingestion.config import IngestionConfig  # noqa: E402
from ingestion.sync_service import SyncService  # noqa: E402

DEBUG_HEADER = "X-Debug-User-Id"
ADMIN = "11111111-1111-1111-1111-111111111111"
READER = "22222222-2222-2222-2222-222222222222"
#: "예산" in CP949, as a legacy Windows share would store it.
CP949_NAME = os.fsdecode("예산계획".encode("cp949") + b".hwp")


@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def relocate_db(server) -> str:
    return pgtest.migrated_database(server, "relocate_test")


@pytest.fixture
def dsn(relocate_db) -> str:
    url = pgtest.psycopg_url(relocate_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def factory(dsn):
    return lambda: psycopg.connect(dsn)


@pytest.fixture
def root(tmp_path) -> Path:
    shared = tmp_path / "shared"
    (shared / "HELLO").mkdir(parents=True)
    (shared / "사업B").mkdir()
    (shared / "HELLO" / "완료보고서.hwp").write_bytes(b"REPORT-BYTES")
    (shared / "HELLO" / CP949_NAME).write_bytes(b"LEGACY-BYTES")
    return shared


def ingest(factory, root):
    return SyncService(factory, IngestionConfig(shared_root=root)).scan_once()


@pytest.fixture
def world(factory, conn, root):
    ingest(factory, root)
    conn.execute("""
        UPDATE document_revisions
        SET parse_status = 'SUCCESS', parse_result_code = 'TEXT_EXTRACTED',
            embedding_status = 'SUCCESS'
    """)
    conn.execute("UPDATE documents SET current_revision_id = latest_revision_id")
    conn.execute("UPDATE processing_jobs SET status = 'SUCCESS'")
    conn.execute(
        "INSERT INTO users (id, sso_subject, name, is_system_admin) VALUES (%s, 'admin', 'admin', TRUE)",
        (ADMIN,),
    )
    conn.execute(
        "INSERT INTO users (id, sso_subject, name, is_system_admin) VALUES (%s, 'reader', 'reader', FALSE)",
        (READER,),
    )
    conn.execute("""
        INSERT INTO document_permissions (document_id, is_public, permission)
        SELECT id, TRUE, 'READ' FROM documents
    """)
    return root


@pytest.fixture
def client(dsn, root, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("SHARED_ROOT", str(root))
    monkeypatch.setenv("SHARED_WRITE_ROOT", str(root))
    monkeypatch.setenv("DOCUMENT_FILE_MANAGEMENT_ENABLED", "true")
    from api import dependencies
    from api.app import create_app

    dependencies.get_config.cache_clear()
    dependencies.get_dsn.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    dependencies.get_config.cache_clear()
    dependencies.get_dsn.cache_clear()


def document(conn, fragment: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, source_path, title, original_filename,
                   current_revision_id::text, latest_revision_id::text,
                   (SELECT count(*) FROM document_revisions r WHERE r.document_id = d.id)
            FROM documents d WHERE source_path LIKE %s
            """,
            (f"%{fragment}%",),
        )
        row = cur.fetchone()
    keys = ("id", "source_path", "title", "original_filename", "current", "latest", "revisions")
    return dict(zip(keys, row)) if row else None


def relocate(client, document_id, user=ADMIN, **body):
    return client.post(f"/api/v1/documents/{document_id}/relocate", json=body,
                       headers={DEBUG_HEADER: user})


def counts(conn) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM document_revisions)")
        return cur.fetchone()


class TestRenameAndMove:
    def test_rename_changes_the_real_file_and_keeps_the_document(self, client, conn, world):
        before = document(conn, "완료보고서")
        response = relocate(client, before["id"], filename="최종완료보고서.hwp")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["renamed"] is True and body["moved"] is False
        assert body["location"] == {"file_name": "최종완료보고서.hwp", "folder_path": "HELLO",
                                    "folder_name": "HELLO"}

        assert (world / "HELLO" / "최종완료보고서.hwp").read_bytes() == b"REPORT-BYTES"
        assert not (world / "HELLO" / "완료보고서.hwp").exists()
        after = document(conn, "최종완료보고서")
        assert after["id"] == before["id"]
        assert after["revisions"] == before["revisions"]
        assert (after["current"], after["latest"]) == (before["current"], before["latest"])
        assert after["source_path"] == "HELLO/최종완료보고서.hwp"
        assert after["title"] == "최종완료보고서"

    def test_source_path_at_ingest_is_provenance_and_stays(self, client, conn, world):
        before = document(conn, "완료보고서")
        relocate(client, before["id"], filename="새이름.hwp", folder_path="사업B")
        with conn.cursor() as cur:
            cur.execute("SELECT source_path_at_ingest FROM document_revisions WHERE document_id = %s",
                        (before["id"],))
            assert [r[0] for r in cur.fetchall()] == ["HELLO/완료보고서.hwp"]

    def test_move_changes_the_real_folder_and_keeps_the_document(self, client, conn, world):
        before = document(conn, "완료보고서")
        response = relocate(client, before["id"], folder_path="사업B")
        assert response.status_code == 200, response.text
        assert response.json()["moved"] is True and response.json()["renamed"] is False
        assert (world / "사업B" / "완료보고서.hwp").read_bytes() == b"REPORT-BYTES"
        after = document(conn, "완료보고서")
        assert (after["id"], after["revisions"]) == (before["id"], before["revisions"])
        assert after["source_path"] == "사업B/완료보고서.hwp"

    def test_rename_and_move_together(self, client, conn, world):
        before = document(conn, "완료보고서")
        body = relocate(client, before["id"], filename="이동본.hwp", folder_path="사업B").json()
        assert body["renamed"] is True and body["moved"] is True
        assert (world / "사업B" / "이동본.hwp").exists()
        after = document(conn, "이동본")
        assert (after["id"], after["revisions"]) == (before["id"], before["revisions"])

    def test_moving_to_the_top_level(self, client, conn, world):
        before = document(conn, "완료보고서")
        assert relocate(client, before["id"], folder_path="").status_code == 200
        assert (world / "완료보고서.hwp").exists()
        assert document(conn, "완료보고서")["source_path"] == "완료보고서.hwp"

    def test_same_values_are_a_no_op(self, client, conn, world):
        before = document(conn, "완료보고서")
        body = relocate(client, before["id"], filename="완료보고서.hwp", folder_path="HELLO").json()
        assert body["changed"] is False
        assert document(conn, "완료보고서")["source_path"] == before["source_path"]
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_logs WHERE target_type = 'DOCUMENT'")
            assert cur.fetchone()[0] == 0

    def test_a_legacy_cp949_name_keeps_its_bytes_when_moved(self, client, conn, world):
        raw = CP949_NAME.encode("utf-8", "surrogateescape")
        before = document(conn, "%BF%B9")
        assert before is not None
        response = relocate(client, before["id"], folder_path="사업B")
        assert response.status_code == 200, response.text
        assert raw in os.listdir(os.fsencode(str(world / "사업B")))
        after = document(conn, "%BF%B9")
        assert after["id"] == before["id"]
        assert after["source_path"].startswith("사업B/%")


class TestRefusals:
    def test_an_existing_destination_is_never_overwritten(self, client, conn, world):
        (world / "사업B" / "완료보고서.hwp").write_bytes(b"OTHER-FILE")
        before = document(conn, "HELLO/완료보고서")
        response = relocate(client, before["id"], folder_path="사업B")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "FILE_ALREADY_EXISTS"
        assert response.json()["error"]["message"] == "같은 이름의 파일이 이미 존재합니다."
        assert (world / "HELLO" / "완료보고서.hwp").read_bytes() == b"REPORT-BYTES"
        assert (world / "사업B" / "완료보고서.hwp").read_bytes() == b"OTHER-FILE"
        assert document(conn, "HELLO/완료보고서")["source_path"] == before["source_path"]

    @pytest.mark.parametrize("folder_path", ["../../etc", "/etc", "HELLO/../..", "..", "HELLO\\..", "없는폴더"])
    def test_a_bad_destination_folder_is_refused(self, client, conn, world, folder_path):
        doc = document(conn, "완료보고서")
        response = relocate(client, doc["id"], folder_path=folder_path)
        assert response.status_code == 422, (folder_path, response.text)
        assert (world / "HELLO" / "완료보고서.hwp").exists()

    def test_a_symlink_leading_out_of_the_root_is_refused(self, client, conn, world, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (world / "탈출").symlink_to(outside, target_is_directory=True)
        doc = document(conn, "완료보고서")
        assert relocate(client, doc["id"], folder_path="탈출").status_code == 422
        assert list(outside.iterdir()) == []

    def test_a_symlink_inside_the_root_is_refused_too(self, client, conn, world):
        (world / "링크").symlink_to(world / "사업B", target_is_directory=True)
        doc = document(conn, "완료보고서")
        assert relocate(client, doc["id"], folder_path="링크").status_code == 422
        assert (world / "HELLO" / "완료보고서.hwp").exists()

    @pytest.mark.parametrize("filename", [
        "../완료보고서.hwp", "a/b.hwp", "a\\\\b.hwp", "", "   ", "\x00.hwp", ".hwp", "..",
    ])
    def test_a_bad_filename_is_refused(self, client, conn, world, filename):
        doc = document(conn, "완료보고서")
        response = relocate(client, doc["id"], filename=filename)
        assert response.status_code == 422, (filename, response.text)
        assert (world / "HELLO" / "완료보고서.hwp").exists()

    def test_changing_the_extension_is_refused(self, client, conn, world):
        doc = document(conn, "완료보고서")
        response = relocate(client, doc["id"], filename="완료보고서.pdf")
        assert response.status_code == 422
        assert (world / "HELLO" / "완료보고서.hwp").exists()

    def test_an_ordinary_user_is_refused(self, client, conn, world):
        doc = document(conn, "완료보고서")
        response = relocate(client, doc["id"], user=READER, filename="바꿈.hwp")
        assert response.status_code == 403
        assert (world / "HELLO" / "완료보고서.hwp").exists()

    def test_nothing_changes_while_the_feature_is_off(self, client, conn, world, monkeypatch):
        monkeypatch.setenv("DOCUMENT_FILE_MANAGEMENT_ENABLED", "false")
        doc = document(conn, "완료보고서")
        response = relocate(client, doc["id"], filename="바꿈.hwp")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "FEATURE_UNAVAILABLE"
        assert (world / "HELLO" / "완료보고서.hwp").exists()

    def test_a_document_being_processed_is_refused(self, client, factory, conn, world):
        # A new version saved in place: sync adds a revision and a PARSE job.
        (world / "HELLO" / "완료보고서.hwp").write_bytes(b"REPORT-BYTES-V2")
        ingest(factory, world)
        doc = document(conn, "완료보고서")
        response = relocate(client, doc["id"], filename="바꿈.hwp")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DOCUMENT_PROCESSING"
        assert (world / "HELLO" / "완료보고서.hwp").exists()

    def test_an_unknown_body_field_is_refused(self, client, conn, world):
        doc = document(conn, "완료보고서")
        response = relocate(client, doc["id"], path="/etc/passwd")
        assert response.status_code == 422


class TestIdentityAndReconciliation:
    def test_references_follow_the_document(self, client, conn, world):
        doc = document(conn, "완료보고서")
        conn.execute("INSERT INTO favorites (user_id, document_id) VALUES (%s, %s)", (READER, doc["id"]))
        conn.execute("INSERT INTO chat_sessions (user_id, title, document_id) VALUES (%s, 't', %s)",
                     (READER, doc["id"]))
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM document_permissions WHERE document_id = %s", (doc["id"],))
            permissions = cur.fetchone()[0]

        assert relocate(client, doc["id"], filename="이동본.hwp", folder_path="사업B").status_code == 200

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM favorites WHERE document_id = %s", (doc["id"],))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM chat_sessions WHERE document_id = %s", (doc["id"],))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM document_permissions WHERE document_id = %s", (doc["id"],))
            assert cur.fetchone()[0] == permissions

    def test_the_next_scan_sees_nothing_new(self, client, factory, conn, world):
        doc = document(conn, "완료보고서")
        totals = counts(conn)
        assert relocate(client, doc["id"], filename="이동본.hwp", folder_path="사업B").status_code == 200

        result = ingest(factory, world)
        assert (result.new_documents, result.new_revisions, result.renamed, result.missing) == (0, 0, 0, 0)
        assert counts(conn) == totals
        assert document(conn, "이동본")["id"] == doc["id"]

    def test_every_change_is_audited_without_body_text(self, client, conn, world):
        doc = document(conn, "완료보고서")
        relocate(client, doc["id"], filename="이동본.hwp", folder_path="사업B")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT action, actor_user_id::text, target_type, target_id::text, metadata
                FROM audit_logs WHERE target_type = 'DOCUMENT' ORDER BY action
            """)
            rows = cur.fetchall()
        assert [r[0] for r in rows] == ["DOCUMENT_MOVED", "DOCUMENT_RENAMED"]
        for action, actor, target_type, target_id, metadata in rows:
            assert (actor, target_type, target_id) == (ADMIN, "DOCUMENT", doc["id"])
            assert metadata == {"from_path": "HELLO/완료보고서.hwp", "to_path": "사업B/이동본.hwp"}
            assert "REPORT-BYTES" not in json.dumps(metadata)

    def test_detail_tells_only_an_admin_the_actions_are_available(self, client, conn, world, monkeypatch):
        doc = document(conn, "완료보고서")
        admin = client.get(f"/api/v1/documents/{doc['id']}", headers={DEBUG_HEADER: ADMIN}).json()
        reader = client.get(f"/api/v1/documents/{doc['id']}", headers={DEBUG_HEADER: READER}).json()
        assert admin["file_management"] == {"available": True}
        assert reader["file_management"] == {"available": False}
        assert admin["location"] == {"file_name": "완료보고서.hwp", "folder_path": "HELLO",
                                     "folder_name": "HELLO"}
        assert str(world) not in json.dumps(admin)

        monkeypatch.setenv("DOCUMENT_FILE_MANAGEMENT_ENABLED", "false")
        off = client.get(f"/api/v1/documents/{doc['id']}", headers={DEBUG_HEADER: ADMIN}).json()
        assert off["file_management"] == {"available": False}

    def test_ingestion_never_reads_the_write_root(self, monkeypatch, tmp_path):
        from ingestion.config import config_from_env

        monkeypatch.setenv("SHARED_ROOT", str(tmp_path))
        monkeypatch.setenv("SHARED_WRITE_ROOT", "/somewhere/else")
        assert config_from_env().shared_root == tmp_path
