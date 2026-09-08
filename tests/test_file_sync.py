"""File Sync integration tests against a real, migrated PostgreSQL.

Every scenario runs on the production schema created by
``alembic upgrade head`` -- never a hand-written subset and never SQLite, whose
missing CHECK constraints and composite FKs would make a green run meaningless.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from ingestion.config import IngestionConfig
from ingestion.sync_service import SyncService

from support import pgtest


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
    return pgtest.migrated_database(server, "ingestion_test")


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


def make_sync(connection_factory, shared_root, **kwargs) -> SyncService:
    config = IngestionConfig(shared_root=shared_root, **kwargs)
    return SyncService(connection_factory, config)


def write(path: Path, content: bytes = b"content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def counts(conn) -> dict[str, int]:
    out = {}
    with conn.cursor() as cur:
        for table in ("documents", "document_revisions", "processing_jobs", "chunks"):
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
    return out


def one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Scenario A -- new file
# ---------------------------------------------------------------------------

class TestScenarioNew:
    def test_new_file_creates_document_revision_and_job(self, connection_factory, conn, shared_root):
        write(shared_root / "2026년 사업계획서.hwp", b"body-a")
        result = make_sync(connection_factory, shared_root).scan_once()

        assert result.discovered == 1
        assert result.new_documents == 1
        assert result.new_revisions == 1
        assert counts(conn) == {
            "documents": 1, "document_revisions": 1, "processing_jobs": 1, "chunks": 0
        }

    def test_document_fields_come_from_the_file(self, connection_factory, conn, shared_root):
        write(shared_root / "부서" / "2026년 사업계획서.hwp")
        make_sync(connection_factory, shared_root).scan_once()

        with conn.cursor() as cur:
            cur.execute("SELECT title, original_filename, source_path, file_type FROM documents")
            title, filename, source_path, file_type = cur.fetchone()
        assert title == "2026년 사업계획서"
        assert filename == "2026년 사업계획서.hwp"
        assert file_type == "hwp"
        # Relative, POSIX, inside the shared root -- never an absolute path.
        assert source_path == "부서/2026년 사업계획서.hwp"

    def test_absolute_path_is_never_stored(self, connection_factory, conn, shared_root):
        write(shared_root / "a.hwp")
        make_sync(connection_factory, shared_root).scan_once()
        stored = one(conn, "SELECT source_path FROM documents")
        stored_at_ingest = one(conn, "SELECT source_path_at_ingest FROM document_revisions")
        assert not stored.startswith("/")
        assert not stored_at_ingest.startswith("/")
        assert str(shared_root) not in stored

    def test_latest_is_set_but_current_is_not(self, connection_factory, conn, shared_root):
        write(shared_root / "a.hwp")
        make_sync(connection_factory, shared_root).scan_once()
        with conn.cursor() as cur:
            cur.execute("SELECT latest_revision_id, current_revision_id FROM documents")
            latest, current = cur.fetchone()
        assert latest is not None
        # Promotion requires READY, which requires embedding -- a later stage.
        assert current is None

    def test_parse_job_is_pending(self, connection_factory, conn, shared_root):
        write(shared_root / "a.hwp")
        make_sync(connection_factory, shared_root).scan_once()
        with conn.cursor() as cur:
            cur.execute("SELECT job_type, status, attempt_count FROM processing_jobs")
            job_type, status, attempts = cur.fetchone()
        assert (job_type, status, attempts) == ("PARSE", "PENDING", 0)

    def test_document_year_is_extracted(self, connection_factory, conn, shared_root):
        write(shared_root / "2026년 사업계획서.hwp")
        write(shared_root / "회의록_최종.hwp", b"other")
        make_sync(connection_factory, shared_root).scan_once()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.title, r.document_year
                FROM document_revisions r JOIN documents d ON d.id = r.document_id
                ORDER BY d.title
            """)
            rows = dict(cur.fetchall())
        assert rows["2026년 사업계획서"] == 2026
        assert rows["회의록_최종"] is None


# ---------------------------------------------------------------------------
# Scenario B -- unchanged / idempotency
# ---------------------------------------------------------------------------

class TestScenarioUnchanged:
    def test_repeated_scans_change_nothing(self, connection_factory, conn, shared_root):
        write(shared_root / "a.hwp")
        write(shared_root / "sub" / "b.hwpx")
        sync = make_sync(connection_factory, shared_root)

        sync.scan_once()
        baseline = counts(conn)
        for _ in range(3):
            result = sync.scan_once()
            assert result.new_documents == 0
            assert result.new_revisions == 0
            assert result.unchanged == 2
        assert counts(conn) == baseline

    def test_mtime_only_change_creates_no_revision(self, connection_factory, conn, shared_root):
        import os
        import time

        path = write(shared_root / "a.hwp", b"same bytes")
        sync = make_sync(connection_factory, shared_root)
        sync.scan_once()

        # Touch the file: mtime moves, content does not.
        future = time.time() + 120
        os.utime(path, (future, future))

        result = sync.scan_once()
        assert result.new_revisions == 0
        assert result.unchanged == 1
        assert counts(conn)["document_revisions"] == 1

    def test_no_duplicate_active_parse_job(self, connection_factory, conn, shared_root):
        write(shared_root / "a.hwp")
        sync = make_sync(connection_factory, shared_root)
        for _ in range(3):
            sync.scan_once()
        assert counts(conn)["processing_jobs"] == 1


# ---------------------------------------------------------------------------
# Scenario C -- modified
# ---------------------------------------------------------------------------

class TestScenarioModified:
    def test_content_change_adds_a_revision_to_the_same_document(
        self, connection_factory, conn, shared_root
    ):
        path = write(shared_root / "a.hwp", b"version one")
        sync = make_sync(connection_factory, shared_root)
        sync.scan_once()
        document_id = one(conn, "SELECT id FROM documents")

        path.write_bytes(b"version two -- different bytes")
        result = sync.scan_once()

        assert result.new_documents == 0
        assert result.new_revisions == 1
        assert one(conn, "SELECT id FROM documents") == document_id
        assert counts(conn)["document_revisions"] == 2

    def test_revision_numbers_increment(self, connection_factory, conn, shared_root):
        path = write(shared_root / "a.hwp", b"v1")
        sync = make_sync(connection_factory, shared_root)
        sync.scan_once()
        for n in range(2, 4):
            path.write_bytes(f"v{n}".encode())
            sync.scan_once()
        with conn.cursor() as cur:
            cur.execute("SELECT revision_no FROM document_revisions ORDER BY revision_no")
            assert [r[0] for r in cur.fetchall()] == [1, 2, 3]

    def test_latest_moves_but_current_is_untouched(self, connection_factory, conn, shared_root):
        path = write(shared_root / "a.hwp", b"v1")
        sync = make_sync(connection_factory, shared_root)
        sync.scan_once()

        # Simulate an earlier revision having been promoted by a later stage.
        first_revision = one(conn, "SELECT id FROM document_revisions")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET current_revision_id = %s", (first_revision,)
            )

        path.write_bytes(b"v2 content")
        sync.scan_once()

        with conn.cursor() as cur:
            cur.execute("SELECT latest_revision_id, current_revision_id FROM documents")
            latest, current = cur.fetchone()
        assert str(current) == str(first_revision), "current must not follow latest"
        assert str(latest) != str(first_revision)

    def test_each_new_revision_gets_its_own_job(self, connection_factory, conn, shared_root):
        path = write(shared_root / "a.hwp", b"v1")
        sync = make_sync(connection_factory, shared_root)
        sync.scan_once()
        # Finish the first job so the partial unique index allows the next one.
        with conn.cursor() as cur:
            cur.execute("UPDATE processing_jobs SET status='SUCCESS', result_code='TEXT_EXTRACTED'")
        path.write_bytes(b"v2")
        sync.scan_once()
        assert counts(conn)["processing_jobs"] == 2


# ---------------------------------------------------------------------------
# Scenario D -- duplicate content
# ---------------------------------------------------------------------------

class TestScenarioDuplicateContent:
    def test_same_bytes_at_two_paths_are_two_documents(
        self, connection_factory, conn, shared_root
    ):
        write(shared_root / "A" / "file.hwp", b"identical bytes")
        write(shared_root / "B" / "copy.hwp", b"identical bytes")
        make_sync(connection_factory, shared_root).scan_once()

        assert counts(conn)["documents"] == 2
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT content_hash FROM document_revisions")
            assert len(cur.fetchall()) == 1, "both revisions share one content hash"

    def test_content_hash_is_not_unique(self, connection_factory, conn, shared_root):
        write(shared_root / "a.hwp", b"dup")
        write(shared_root / "b.hwp", b"dup")
        make_sync(connection_factory, shared_root).scan_once()
        assert counts(conn)["document_revisions"] == 2


# ---------------------------------------------------------------------------
# Scenario E -- missing
# ---------------------------------------------------------------------------

class TestScenarioMissing:
    def test_missing_file_is_marked_not_deleted_during_grace(
        self, connection_factory, conn, shared_root
    ):
        path = write(shared_root / "a.hwp")
        sync = make_sync(connection_factory, shared_root, missing_grace_seconds=3600)
        sync.scan_once()
        path.unlink()

        result = sync.scan_once()
        assert result.missing == 1
        assert result.soft_deleted == 0
        with conn.cursor() as cur:
            cur.execute("SELECT is_deleted, missing_since FROM documents")
            is_deleted, missing_since = cur.fetchone()
        assert is_deleted is False
        assert missing_since is not None

    def test_missing_beyond_grace_is_soft_deleted(self, connection_factory, conn, shared_root):
        path = write(shared_root / "a.hwp")
        sync = make_sync(connection_factory, shared_root, missing_grace_seconds=0)
        sync.scan_once()
        path.unlink()

        result = sync.scan_once()
        assert result.soft_deleted == 1
        with conn.cursor() as cur:
            cur.execute("SELECT is_deleted, deleted_at FROM documents")
            is_deleted, deleted_at = cur.fetchone()
        assert is_deleted is True
        assert deleted_at is not None

    def test_nothing_is_hard_deleted(self, connection_factory, conn, shared_root):
        path = write(shared_root / "a.hwp")
        sync = make_sync(connection_factory, shared_root, missing_grace_seconds=0)
        sync.scan_once()
        before = counts(conn)
        path.unlink()
        sync.scan_once()

        after = counts(conn)
        assert after["documents"] == before["documents"]
        assert after["document_revisions"] == before["document_revisions"]

    def test_reappearing_file_is_restored(self, connection_factory, conn, shared_root):
        path = write(shared_root / "a.hwp", b"body")
        sync = make_sync(connection_factory, shared_root, missing_grace_seconds=0)
        sync.scan_once()
        path.unlink()
        sync.scan_once()
        assert one(conn, "SELECT is_deleted FROM documents") is True

        write(shared_root / "a.hwp", b"body")
        result = sync.scan_once()

        assert result.restored == 1
        with conn.cursor() as cur:
            cur.execute("SELECT is_deleted, deleted_at, missing_since FROM documents")
            is_deleted, deleted_at, missing_since = cur.fetchone()
        assert (is_deleted, deleted_at, missing_since) == (False, None, None)
        # Same content -> no new revision on restore.
        assert counts(conn)["document_revisions"] == 1

    def test_reappearing_with_new_content_adds_a_revision(
        self, connection_factory, conn, shared_root
    ):
        path = write(shared_root / "a.hwp", b"old")
        sync = make_sync(connection_factory, shared_root, missing_grace_seconds=0)
        sync.scan_once()
        path.unlink()
        sync.scan_once()
        write(shared_root / "a.hwp", b"new content entirely")
        sync.scan_once()

        assert counts(conn)["document_revisions"] == 2
        assert one(conn, "SELECT is_deleted FROM documents") is False


# ---------------------------------------------------------------------------
# Move / rename policy
# ---------------------------------------------------------------------------

class TestMoveRenamePolicy:
    def test_moved_file_becomes_a_new_document_and_the_old_one_goes_missing(
        self, connection_factory, conn, shared_root
    ):
        """Conservative by design.

        Identical bytes at a new path could be a move or an independent copy.
        Merging them on hash alone would fuse two unrelated documents and their
        histories, which cannot be undone. Creating a new document is the
        recoverable mistake.
        """
        path = write(shared_root / "old" / "a.hwp", b"same bytes")
        sync = make_sync(connection_factory, shared_root, missing_grace_seconds=3600)
        sync.scan_once()

        path.rename(shared_root / "old" / "renamed.hwp")
        result = sync.scan_once()

        assert result.new_documents == 1
        assert result.missing == 1
        assert counts(conn)["documents"] == 2
        with conn.cursor() as cur:
            cur.execute("SELECT source_path, missing_since IS NOT NULL FROM documents ORDER BY source_path")
            rows = dict(cur.fetchall())
        assert rows["old/a.hwp"] is True
        assert rows["old/renamed.hwp"] is False


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class TestScanRobustness:
    def test_one_bad_file_does_not_abort_the_scan(self, connection_factory, conn, shared_root, monkeypatch):
        write(shared_root / "a.hwp", b"one")
        write(shared_root / "b.hwp", b"two")
        write(shared_root / "c.hwp", b"three")

        import ingestion.sync_service as module

        real = module.fingerprint
        def flaky(path):
            if path.name == "b.hwp":
                raise OSError("simulated read failure")
            return real(path)

        monkeypatch.setattr(module, "fingerprint", flaky)
        result = make_sync(connection_factory, shared_root).scan_once()

        assert result.errors == 1
        assert result.new_documents == 2, "the other files must still be ingested"
        assert counts(conn)["documents"] == 2

    def test_unstable_file_is_skipped_not_stored(self, connection_factory, conn, shared_root, monkeypatch):
        write(shared_root / "a.hwp")
        import ingestion.sync_service as module
        from ingestion.exceptions import FileUnstableError

        monkeypatch.setattr(
            module, "fingerprint",
            lambda p: (_ for _ in ()).throw(FileUnstableError("changing")),
        )
        result = make_sync(connection_factory, shared_root).scan_once()

        assert result.unstable == 1
        assert counts(conn)["documents"] == 0, "a half-written file must not become a revision"

    def test_symlink_escape_is_not_ingested(self, connection_factory, conn, shared_root, tmp_path):
        import os

        outside = tmp_path / "outside" / "secret.hwp"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"secret")
        os.symlink(outside, shared_root / "link.hwp")

        make_sync(connection_factory, shared_root).scan_once()
        assert counts(conn)["documents"] == 0
