"""Storing the document kind as a namespaced tag.

Real PostgreSQL: the invariant being tested -- exactly one kind tag per
document -- is enforced by a locked read-modify-write, which SQLite could not
reproduce and a mock would not test at all.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from support import builders, pgtest

from ingestion.config import IngestionConfig
from ingestion.document_type import TAG_NAMESPACE
from ingestion.repository import IngestionRepository
from ingestion.sync_service import SyncService


@pytest.fixture(scope="session")
def server():
    pytest.importorskip("pgserver", reason="pgserver is required for these DB tests")
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(f"required PostgreSQL extensions unavailable: {missing}")
    return srv


@pytest.fixture(scope="session")
def doctype_db(server) -> str:
    return pgtest.migrated_database(server, "document_type_test")


@pytest.fixture
def dsn(doctype_db) -> str:
    url = pgtest.psycopg_url(doctype_db)
    with psycopg.connect(url, autocommit=True) as conn:
        pgtest.truncate_all(conn)
    return url


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c


@pytest.fixture
def repo(conn):
    return IngestionRepository(conn)


@pytest.fixture
def shared_root(tmp_path) -> Path:
    root = tmp_path / "shared"
    root.mkdir()
    return root


def make_document(repo, filename: str) -> str:
    return repo.create_document(
        title=filename.rsplit(".", 1)[0],
        original_filename=filename,
        source_path=filename,
        file_type=filename.rsplit(".", 1)[-1],
    )


def kind_tags(conn, document_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.name FROM document_tags dt
        JOIN tags t ON t.id = dt.tag_id
        WHERE dt.document_id = %s AND t.name LIKE %s
        ORDER BY t.name
        """,
        (document_id, TAG_NAMESPACE + "%"),
    ).fetchall()
    return [r[0] for r in rows]


class TestSetDocumentType:
    def test_assigns_exactly_one_kind_tag(self, repo, conn):
        doc = make_document(repo, "사용자매뉴얼.hwp")
        assert repo.set_document_type(doc, f"{TAG_NAMESPACE}매뉴얼") is True
        assert kind_tags(conn, doc) == [f"{TAG_NAMESPACE}매뉴얼"]

    def test_replacing_the_kind_leaves_only_the_new_one(self, repo, conn):
        doc = make_document(repo, "무제.hwp")
        repo.set_document_type(doc, f"{TAG_NAMESPACE}기타")
        repo.set_document_type(doc, f"{TAG_NAMESPACE}보고서")
        assert kind_tags(conn, doc) == [f"{TAG_NAMESPACE}보고서"]

    def test_reapplying_the_same_kind_is_a_no_op(self, repo, conn):
        doc = make_document(repo, "사용자매뉴얼.hwp")
        repo.set_document_type(doc, f"{TAG_NAMESPACE}매뉴얼")
        assert repo.set_document_type(doc, f"{TAG_NAMESPACE}매뉴얼") is False
        assert kind_tags(conn, doc) == [f"{TAG_NAMESPACE}매뉴얼"]

    def test_free_form_tags_are_left_alone(self, repo, conn):
        """Replacing the kind must not disturb a human-assigned tag."""
        doc = make_document(repo, "보고서.hwp")
        free = repo.ensure_tag("보안")
        conn.execute(
            "INSERT INTO document_tags (document_id, tag_id, source) VALUES (%s, %s, 'SYSTEM')",
            (doc, free),
        )
        repo.set_document_type(doc, f"{TAG_NAMESPACE}보고서")
        repo.set_document_type(doc, f"{TAG_NAMESPACE}기타")

        names = conn.execute(
            """
            SELECT t.name FROM document_tags dt JOIN tags t ON t.id = dt.tag_id
            WHERE dt.document_id = %s ORDER BY t.name
            """,
            (doc,),
        ).fetchall()
        assert [r[0] for r in names] == ["종류:기타", "보안"] or \
               sorted(r[0] for r in names) == sorted(["종류:기타", "보안"])

    def test_kind_is_recorded_as_system_not_manual(self, repo, conn):
        """MANUAL requires created_by; a rule has no person to attribute it to."""
        doc = make_document(repo, "보고서.hwp")
        repo.set_document_type(doc, f"{TAG_NAMESPACE}보고서")
        source = conn.execute(
            "SELECT source FROM document_tags WHERE document_id = %s", (doc,)
        ).fetchone()[0]
        assert source == "SYSTEM"

    def test_unknown_document_is_ignored(self, repo):
        assert repo.set_document_type(
            "00000000-0000-0000-0000-000000000000", f"{TAG_NAMESPACE}기타"
        ) is False

    def test_document_type_tag_reads_it_back(self, repo):
        doc = make_document(repo, "제안요청서.hwp")
        repo.set_document_type(doc, f"{TAG_NAMESPACE}제안·입찰 문서")
        assert repo.document_type_tag(doc) == f"{TAG_NAMESPACE}제안·입찰 문서"

    def test_ensure_tag_is_idempotent(self, repo):
        assert repo.ensure_tag("종류:보고서") == repo.ensure_tag("종류:보고서")


class TestSyncAssignsKind:
    def test_new_document_gets_its_kind(self, connection_factory_for, shared_root, conn):
        builders.write_hwpx(shared_root / "사용자매뉴얼-테스트.hwpx", text="본문")
        SyncService(connection_factory_for, IngestionConfig(shared_root=shared_root)).scan_once()

        doc = conn.execute("SELECT id FROM documents").fetchone()[0]
        assert kind_tags(conn, str(doc)) == [f"{TAG_NAMESPACE}매뉴얼"]

    def test_unclassifiable_document_gets_an_explicit_other_tag(
        self, connection_factory_for, shared_root, conn
    ):
        builders.write_hwpx(shared_root / "무제자료.hwpx", text="본문")
        SyncService(connection_factory_for, IngestionConfig(shared_root=shared_root)).scan_once()

        doc = conn.execute("SELECT id FROM documents").fetchone()[0]
        # An absent tag would be indistinguishable from "not classified yet".
        assert kind_tags(conn, str(doc)) == [f"{TAG_NAMESPACE}기타"]

    def test_rescanning_does_not_accumulate_kind_tags(
        self, connection_factory_for, shared_root, conn
    ):
        builders.write_hwpx(shared_root / "완료보고서.hwpx", text="본문")
        service = SyncService(connection_factory_for, IngestionConfig(shared_root=shared_root))
        service.scan_once()
        service.scan_once()
        service.scan_once()

        doc = conn.execute("SELECT id FROM documents").fetchone()[0]
        assert kind_tags(conn, str(doc)) == [f"{TAG_NAMESPACE}보고서"]

    def test_a_renamed_file_is_classified_afresh(
        self, connection_factory_for, shared_root, conn
    ):
        """source_path is the document identity, so a rename is a new document.

        The old row stays until the missing-file grace period retires it; the
        new one is classified from its new name.
        """
        builders.write_hwpx(shared_root / "무제자료.hwpx", text="본문")
        config = IngestionConfig(shared_root=shared_root)
        SyncService(connection_factory_for, config).scan_once()

        (shared_root / "무제자료.hwpx").rename(shared_root / "사용자매뉴얼.hwpx")
        SyncService(connection_factory_for, config).scan_once()

        rows = conn.execute(
            """
            SELECT d.original_filename, t.name
            FROM documents d
            JOIN document_tags dt ON dt.document_id = d.id
            JOIN tags t ON t.id = dt.tag_id AND t.name LIKE %s
            ORDER BY d.original_filename
            """,
            (TAG_NAMESPACE + "%",),
        ).fetchall()
        assert dict(rows) == {
            "무제자료.hwpx": f"{TAG_NAMESPACE}기타",
            "사용자매뉴얼.hwpx": f"{TAG_NAMESPACE}매뉴얼",
        }


@pytest.fixture
def connection_factory_for(dsn):
    def factory():
        return psycopg.connect(dsn)

    return factory
