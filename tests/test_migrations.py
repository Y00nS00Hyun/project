"""Migration tests against a real PostgreSQL.

Everything here runs on an actual PostgreSQL cluster with pgvector and pg_trgm.
SQLite is never substituted: CHECK constraints, composite foreign keys,
generated columns and the `vector` type do not exist there, so a green suite on
SQLite would prove nothing about this schema.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from support import pgtest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DOC = ROOT / "docs" / "database-schema-v2.5.md"

EXPECTED_TABLE_COUNT = 16

#: Alembic's own bookkeeping table is not part of the application schema.
ALEMBIC_TABLE = "alembic_version"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def server():
    pgserver = pytest.importorskip("pgserver", reason="pgserver is required for migration tests")
    del pgserver
    srv = pgtest.start_server()
    missing = pgtest.check_extensions_available(srv)
    if missing:
        pytest.fail(
            f"required PostgreSQL extensions unavailable: {missing}. "
            "Install them; the tests must not silently skip."
        )
    return srv


def run_alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=url)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


@pytest.fixture(scope="session")
def migrated_url(server) -> str:
    """A database with the migration applied once, shared by read-only tests."""
    url = pgtest.create_database(server, "migration_head")
    result = run_alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return url


@pytest.fixture
def conn(migrated_url):
    with psycopg.connect(pgtest.psycopg_url(migrated_url), autocommit=True) as c:
        yield c


@pytest.fixture
def tx(migrated_url):
    """A rolled-back transaction, so constraint tests cannot pollute each other."""
    with psycopg.connect(pgtest.psycopg_url(migrated_url)) as c:
        yield c
        c.rollback()


def scalar(c, sql, params=None):
    with c.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def rows(c, sql, params=None):
    with c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Migration lifecycle
# ---------------------------------------------------------------------------

class TestMigrationLifecycle:
    def test_fresh_upgrade_downgrade_reupgrade(self, server):
        """Empty DB -> head -> base -> head, all on a database of its own."""
        url = pgtest.create_database(server, "migration_lifecycle")

        up = run_alembic(url, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        with psycopg.connect(pgtest.psycopg_url(url), autocommit=True) as c:
            assert self._app_table_count(c) == EXPECTED_TABLE_COUNT

        down = run_alembic(url, "downgrade", "base")
        assert down.returncode == 0, down.stderr
        with psycopg.connect(pgtest.psycopg_url(url), autocommit=True) as c:
            assert self._app_table_count(c) == 0, "downgrade left application tables behind"
            assert scalar(c, "SELECT count(*) FROM information_schema.views WHERE table_schema='public'") == 0

        again = run_alembic(url, "upgrade", "head")
        assert again.returncode == 0, again.stderr
        with psycopg.connect(pgtest.psycopg_url(url), autocommit=True) as c:
            assert self._app_table_count(c) == EXPECTED_TABLE_COUNT

    def test_downgrade_keeps_extensions(self, server):
        """Extensions are database-wide; downgrade must not drop them."""
        url = pgtest.create_database(server, "migration_ext")
        assert run_alembic(url, "upgrade", "head").returncode == 0
        assert run_alembic(url, "downgrade", "base").returncode == 0
        with psycopg.connect(pgtest.psycopg_url(url), autocommit=True) as c:
            present = {r[0] for r in rows(c, "SELECT extname FROM pg_extension")}
        assert {"vector", "pg_trgm"} <= present

    @staticmethod
    def _app_table_count(c) -> int:
        return scalar(
            c,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name <> %s",
            (ALEMBIC_TABLE,),
        )


class TestSchemaShape:
    def test_application_table_count(self, conn):
        assert TestMigrationLifecycle._app_table_count(conn) == EXPECTED_TABLE_COUNT

    def test_expected_tables_exist(self, conn):
        expected = {
            "departments", "users", "documents", "document_revisions", "chunks",
            "document_permissions", "processing_jobs", "tags", "document_tags",
            "revision_tags", "favorites", "recent_views", "chat_sessions",
            "chat_messages", "chat_message_sources", "audit_logs",
        }
        actual = {
            r[0] for r in rows(
                conn,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name <> %s",
                (ALEMBIC_TABLE,),
            )
        }
        assert actual == expected

    def test_current_ready_chunks_view_exists(self, conn):
        assert scalar(
            conn,
            "SELECT count(*) FROM information_schema.views "
            "WHERE table_schema='public' AND table_name='current_ready_chunks'",
        ) == 1

    def test_no_duplicate_indexes(self, conn):
        """Two indexes on the same table with the same column list are waste."""
        found = rows(conn, """
            SELECT tablename, regexp_replace(indexdef, '^CREATE (UNIQUE )?INDEX \\S+ ', ''), count(*)
            FROM pg_indexes WHERE schemaname='public'
            GROUP BY 1,2 HAVING count(*) > 1
        """)
        assert found == [], f"duplicate index definitions: {found}"


class TestExtensions:
    def test_required_extensions_installed(self, conn):
        present = {r[0] for r in rows(conn, "SELECT extname FROM pg_extension")}
        assert {"vector", "pg_trgm", "pgcrypto"} <= present

    def test_pg_trgm_operators_usable(self, conn):
        assert scalar(conn, "SELECT similarity(%s, %s)", ("문서관리시스템", "문서 관리 시스템")) > 0
        # `<%` is the word_similarity threshold operator the lexical route uses.
        assert scalar(conn, "SELECT %s <%% %s", ("정보보안", "정보보안팀 시스템 운영지침")) is True
        assert scalar(conn, "SELECT word_similarity(%s, %s) > 0",
                      ("정보보안", "정보보안팀 시스템 운영지침")) is True

    def test_trigram_indexes_exist_with_gin_opclass(self, conn):
        defs = {
            r[0]: r[1] for r in rows(
                conn,
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND indexdef ILIKE '%%trgm%%'",
            )
        }
        assert "idx_documents_title_trgm" in defs
        assert "idx_revisions_extracted_text_trgm" in defs
        for name, ddl in defs.items():
            assert "gin_trgm_ops" in ddl, f"{name} is not gin_trgm_ops: {ddl}"
            assert "USING gin" in ddl, f"{name} is not a GIN index: {ddl}"


class TestVectorColumn:
    def test_embedding_is_384_dimensions(self, conn):
        ddl = scalar(conn, """
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relname='chunks' AND a.attname='embedding'
        """)
        assert ddl == "vector(384)"

    def test_384_dimension_vector_inserts(self, tx, seed):
        revision_id = seed["revision_id"]
        vec = "[" + ",".join("0.01" for _ in range(384)) + "]"
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text, embedding) "
                "VALUES (%s, 900, 'ok', %s::vector)",
                (revision_id, vec),
            )

    def test_wrong_dimension_vector_is_rejected(self, tx, seed):
        revision_id = seed["revision_id"]
        vec = "[" + ",".join("0.01" for _ in range(128)) + "]"
        with pytest.raises(psycopg.errors.DataException), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text, embedding) "
                "VALUES (%s, 901, 'bad', %s::vector)",
                (revision_id, vec),
            )

    def test_no_hnsw_index_exists(self, conn):
        found = rows(
            conn,
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexdef ILIKE '%%hnsw%%'",
        )
        assert found == [], f"HNSW index must not be created yet: {found}"

    def test_no_fts_gin_index_on_chunks(self, conn):
        found = rows(
            conn,
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND indexdef ILIKE '%%search_vector%%'",
        )
        assert found == [], f"optional FTS index must not be created by default: {found}"


# ---------------------------------------------------------------------------
# Seed data for constraint tests
# ---------------------------------------------------------------------------

@pytest.fixture
def seed(tx):
    """One department / user / document / revision, rolled back after the test."""
    with tx.cursor() as cur:
        cur.execute("INSERT INTO departments (name) VALUES ('기획조정실') RETURNING id")
        dept = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (sso_subject, name, department_id) VALUES ('u1','홍길동',%s) RETURNING id",
            (dept,),
        )
        user = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO documents (title, original_filename, source_path, file_type, department_id) "
            "VALUES ('2026년 사업계획서','plan.hwp','/share/plan.hwp','hwp',%s) RETURNING id",
            (dept,),
        )
        doc = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO document_revisions "
            "(document_id, revision_no, content_hash, source_path_at_ingest, "
            " parse_status, parse_result_code, embedding_status) "
            "VALUES (%s, 1, 'abc', '/share/plan.hwp', 'SUCCESS', 'TEXT_EXTRACTED', 'SUCCESS') RETURNING id",
            (doc,),
        )
        rev = cur.fetchone()[0]
    return {"department_id": dept, "user_id": user, "document_id": doc, "revision_id": rev}


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

class TestRevisionPointerIntegrity:
    def test_cross_document_current_revision_is_rejected(self, tx, seed):
        """documents.current_revision_id must belong to that same document."""
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (title, original_filename, source_path, file_type) "
                "VALUES ('다른 문서','o.hwp','/share/o.hwp','hwp') RETURNING id"
            )
            other = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.ForeignKeyViolation), tx.cursor() as cur:
            # seed's revision belongs to seed's document, not to `other`.
            cur.execute(
                "UPDATE documents SET current_revision_id = %s WHERE id = %s",
                (seed["revision_id"], other),
            )
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_cross_document_latest_revision_is_rejected(self, tx, seed):
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (title, original_filename, source_path, file_type) "
                "VALUES ('다른 문서2','o2.hwp','/share/o2.hwp','hwp') RETURNING id"
            )
            other = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.ForeignKeyViolation), tx.cursor() as cur:
            cur.execute(
                "UPDATE documents SET latest_revision_id = %s WHERE id = %s",
                (seed["revision_id"], other),
            )
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_own_revision_pointer_is_accepted(self, tx, seed):
        with tx.cursor() as cur:
            cur.execute(
                "UPDATE documents SET current_revision_id = %s, latest_revision_id = %s WHERE id = %s",
                (seed["revision_id"], seed["revision_id"], seed["document_id"]),
            )
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")


class TestRevisionStatusDomains:
    def test_invalid_parse_status_is_rejected(self, tx, seed):
        with pytest.raises(psycopg.errors.CheckViolation), tx.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET parse_status = 'SKIPPED' WHERE id = %s",
                (seed["revision_id"],),
            )

    def test_invalid_parse_result_code_is_rejected(self, tx, seed):
        with pytest.raises(psycopg.errors.CheckViolation), tx.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET parse_result_code = 'NOT_A_CODE' WHERE id = %s",
                (seed["revision_id"],),
            )

    @pytest.mark.parametrize("code", [
        "TEXT_EXTRACTED", "EMPTY_DOCUMENT", "OCR_REQUIRED", "ENCRYPTED",
        "CORRUPT", "UNSUPPORTED_FORMAT", "PARSE_FAILED",
    ])
    def test_every_documented_result_code_is_accepted(self, tx, seed, code):
        with tx.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET parse_result_code = %s WHERE id = %s",
                (code, seed["revision_id"]),
            )

    @pytest.mark.parametrize("column", ["embedding_status", "summary_status", "tagging_status"])
    def test_downstream_status_accepts_skipped(self, tx, seed, column):
        with tx.cursor() as cur:
            cur.execute(
                f"UPDATE document_revisions SET {column} = 'SKIPPED' WHERE id = %s",
                (seed["revision_id"],),
            )


class TestIsReady:
    """is_ready is a generated column; verify the real expression, not a copy of it."""

    def _is_ready(self, tx, seed, parse, code, embedding):
        with tx.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET parse_status=%s, parse_result_code=%s, "
                "embedding_status=%s WHERE id=%s RETURNING is_ready",
                (parse, code, embedding, seed["revision_id"]),
            )
            return cur.fetchone()[0]

    def test_ready_when_text_extracted_and_embedded(self, tx, seed):
        assert self._is_ready(tx, seed, "SUCCESS", "TEXT_EXTRACTED", "SUCCESS") is True

    def test_not_ready_for_ocr_required(self, tx, seed):
        assert self._is_ready(tx, seed, "SUCCESS", "OCR_REQUIRED", "SKIPPED") is False

    def test_not_ready_when_result_code_is_null(self, tx, seed):
        """NULL must yield FALSE, not NULL -- this is what COALESCE guards."""
        value = self._is_ready(tx, seed, "SUCCESS", None, "SUCCESS")
        assert value is False, f"expected FALSE for NULL result code, got {value!r}"

    def test_not_ready_when_embedding_pending(self, tx, seed):
        assert self._is_ready(tx, seed, "SUCCESS", "TEXT_EXTRACTED", "PENDING") is False

    def test_not_ready_when_parse_failed(self, tx, seed):
        assert self._is_ready(tx, seed, "FAILED", None, "PENDING") is False


class TestDocumentYear:
    """v2.5 / OI-1."""

    def test_null_is_accepted(self, tx, seed):
        with tx.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET document_year = NULL WHERE id=%s", (seed["revision_id"],)
            )

    @pytest.mark.parametrize("year", [1900, 2026, 2100])
    def test_in_range_years_accepted(self, tx, seed, year):
        with tx.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET document_year=%s WHERE id=%s",
                (year, seed["revision_id"]),
            )

    @pytest.mark.parametrize("year", [1800, 1899, 2101, 3000])
    def test_out_of_range_years_rejected(self, tx, seed, year):
        with pytest.raises(psycopg.errors.CheckViolation), tx.cursor() as cur:
            cur.execute(
                "UPDATE document_revisions SET document_year=%s WHERE id=%s",
                (year, seed["revision_id"]),
            )

    def test_column_is_smallint(self, conn):
        assert scalar(conn, """
            SELECT data_type FROM information_schema.columns
            WHERE table_name='document_revisions' AND column_name='document_year'
        """) == "smallint"


class TestPermissions:
    def test_both_subjects_null_is_rejected(self, tx, seed):
        with pytest.raises(psycopg.errors.CheckViolation), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO document_permissions (document_id, permission) VALUES (%s,'READ')",
                (seed["document_id"],),
            )

    def test_both_subjects_set_is_rejected(self, tx, seed):
        with pytest.raises(psycopg.errors.CheckViolation), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO document_permissions (document_id, user_id, department_id, permission) "
                "VALUES (%s,%s,%s,'READ')",
                (seed["document_id"], seed["user_id"], seed["department_id"]),
            )

    def test_exactly_one_subject_is_accepted(self, tx, seed):
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO document_permissions (document_id, user_id, permission) VALUES (%s,%s,'READ')",
                (seed["document_id"], seed["user_id"]),
            )
            cur.execute(
                "INSERT INTO document_permissions (document_id, department_id, permission) VALUES (%s,%s,'READ')",
                (seed["document_id"], seed["department_id"]),
            )

    def test_invalid_permission_level_rejected(self, tx, seed):
        with pytest.raises(psycopg.errors.CheckViolation), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO document_permissions (document_id, user_id, permission) VALUES (%s,%s,'DENY')",
                (seed["document_id"], seed["user_id"]),
            )


class TestChunks:
    def test_duplicate_chunk_index_rejected(self, tx, seed):
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text) VALUES (%s,0,'a')",
                (seed["revision_id"],),
            )
        with pytest.raises(psycopg.errors.UniqueViolation), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text) VALUES (%s,0,'b')",
                (seed["revision_id"],),
            )

    def test_paragraph_range_must_be_ordered(self, tx, seed):
        with pytest.raises(psycopg.errors.CheckViolation), tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text, paragraph_start, paragraph_end) "
                "VALUES (%s,10,'x',5,2)",
                (seed["revision_id"],),
            )


class TestChatRefused:
    """v2.5 / OI-2."""

    @pytest.fixture
    def session_id(self, tx, seed):
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_sessions (user_id, title) VALUES (%s,'문의') RETURNING id",
                (seed["user_id"],),
            )
            return cur.fetchone()[0]

    def _insert(self, tx, session_id, role, refused):
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_messages (session_id, role, content, refused) VALUES (%s,%s,'내용',%s)",
                (session_id, role, refused),
            )

    def test_user_message_with_null_refused_accepted(self, tx, session_id):
        self._insert(tx, session_id, "user", None)

    def test_system_message_with_null_refused_accepted(self, tx, session_id):
        self._insert(tx, session_id, "system", None)

    @pytest.mark.parametrize("refused", [True, False])
    def test_assistant_message_requires_a_value(self, tx, session_id, refused):
        self._insert(tx, session_id, "assistant", refused)

    def test_user_message_with_refused_value_rejected(self, tx, session_id):
        with pytest.raises(psycopg.errors.CheckViolation):
            self._insert(tx, session_id, "user", True)

    def test_assistant_message_without_refused_rejected(self, tx, session_id):
        with pytest.raises(psycopg.errors.CheckViolation):
            self._insert(tx, session_id, "assistant", None)

    def test_invalid_role_rejected(self, tx, session_id):
        with pytest.raises(psycopg.errors.CheckViolation):
            self._insert(tx, session_id, "ASSISTANT", True)


class TestProvenanceProtection:
    def test_chunk_used_as_a_chat_source_cannot_be_deleted(self, tx, seed):
        """ON DELETE RESTRICT protects the provenance of past answers."""
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text) VALUES (%s,20,'근거') RETURNING id",
                (seed["revision_id"],),
            )
            chunk = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO chat_sessions (user_id) VALUES (%s) RETURNING id", (seed["user_id"],)
            )
            session = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO chat_messages (session_id, role, content, refused) "
                "VALUES (%s,'assistant','답변',FALSE) RETURNING id",
                (session,),
            )
            message = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO chat_message_sources (message_id, chunk_id) VALUES (%s,%s)",
                (message, chunk),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), tx.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE id = %s", (chunk,))

    def test_content_hash_is_not_unique(self, tx, seed):
        """Identical copies in different folders are legitimate."""
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (title, original_filename, source_path, file_type) "
                "VALUES ('사본','plan.hwp','/share/copy/plan.hwp','hwp') RETURNING id"
            )
            other_doc = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO document_revisions (document_id, revision_no, content_hash, source_path_at_ingest) "
                "VALUES (%s,1,'abc','/share/copy/plan.hwp')",
                (other_doc,),
            )


class TestCurrentReadyChunksView:
    def test_view_excludes_non_current_and_not_ready(self, tx, seed):
        with tx.cursor() as cur:
            cur.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text) VALUES (%s,30,'본문') RETURNING id",
                (seed["revision_id"],),
            )
            chunk = cur.fetchone()[0]
            # Not yet current -> not visible.
            cur.execute("SELECT count(*) FROM current_ready_chunks WHERE chunk_id=%s", (chunk,))
            assert cur.fetchone()[0] == 0

            cur.execute(
                "UPDATE documents SET current_revision_id=%s WHERE id=%s",
                (seed["revision_id"], seed["document_id"]),
            )
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cur.execute("SELECT count(*) FROM current_ready_chunks WHERE chunk_id=%s", (chunk,))
            assert cur.fetchone()[0] == 1

            # Soft-deleted document disappears from the view.
            cur.execute(
                "UPDATE documents SET is_deleted=TRUE, deleted_at=now() WHERE id=%s",
                (seed["document_id"],),
            )
            cur.execute("SELECT count(*) FROM current_ready_chunks WHERE chunk_id=%s", (chunk,))
            assert cur.fetchone()[0] == 0

    def test_view_hides_chunks_of_a_not_ready_revision(self, tx, seed):
        with tx.cursor() as cur:
            cur.execute(
                "UPDATE documents SET current_revision_id=%s WHERE id=%s",
                (seed["revision_id"], seed["document_id"]),
            )
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cur.execute(
                "INSERT INTO chunks (document_revision_id, chunk_index, text) VALUES (%s,31,'본문') RETURNING id",
                (seed["revision_id"],),
            )
            chunk = cur.fetchone()[0]
            cur.execute(
                "UPDATE document_revisions SET embedding_status='SKIPPED' WHERE id=%s",
                (seed["revision_id"],),
            )
            cur.execute("SELECT count(*) FROM current_ready_chunks WHERE chunk_id=%s", (chunk,))
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------

class TestSchemaDrift:
    """The migration must not diverge from docs/database-schema-v2.5.md."""

    @staticmethod
    def documented_ddl() -> list[str]:
        text = SCHEMA_DOC.read_text(encoding="utf-8")
        blocks = re.findall(r"```sql\n(.*?)```", text, re.S)
        keep = re.compile(
            r"^\s*(CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX|VIEW|EXTENSION)|ALTER\s+TABLE)", re.I | re.M
        )
        out = []
        for block in blocks:
            lines = [l for l in block.splitlines() if l.strip()]
            if lines and all(l.strip().startswith("--") for l in lines):
                continue
            if keep.search(block):
                out.append(block)
        return out

    def test_documented_tables_match_database(self, conn):
        documented = {
            m.group(1) for b in self.documented_ddl()
            for m in re.finditer(r"CREATE TABLE (\w+)", b)
        }
        actual = {
            r[0] for r in rows(
                conn,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name <> %s",
                (ALEMBIC_TABLE,),
            )
        }
        assert documented == actual

    def test_documented_indexes_match_database(self, conn):
        documented = {
            m.group(1) for b in self.documented_ddl()
            for m in re.finditer(r"CREATE (?:UNIQUE )?INDEX (\w+)", b)
        }
        actual = {
            r[0] for r in rows(
                conn,
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                "AND indexname NOT LIKE '%%_pkey' AND indexname NOT LIKE '%%_key' "
                "AND tablename <> %s",
                (ALEMBIC_TABLE,),
            )
        }
        assert documented == actual, (
            f"only in doc: {sorted(documented - actual)} / only in DB: {sorted(actual - documented)}"
        )

    def test_documented_views_match_database(self, conn):
        documented = {
            m.group(1) for b in self.documented_ddl() for m in re.finditer(r"CREATE VIEW (\w+)", b)
        }
        actual = {
            r[0] for r in rows(
                conn, "SELECT table_name FROM information_schema.views WHERE table_schema='public'"
            )
        }
        assert documented == actual

    def test_documented_columns_match_database(self, conn):
        """Every column named in a documented CREATE TABLE exists in the database."""
        problems = []
        for block in self.documented_ddl():
            match = re.match(r"\s*CREATE TABLE (\w+) \((.*)\);?\s*$", block, re.S)
            if not match:
                continue
            table, body = match.group(1), match.group(2)
            body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
            documented = set()
            for line in body.splitlines():
                col = re.match(r"\s{4}(\w+)\s+(UUID|TEXT|INT|SMALLINT|BIGINT|BOOLEAN|VECTOR|TSVECTOR|"
                               r"TIMESTAMPTZ|SERIAL|JSONB|DOUBLE)", line)
                if col and col.group(1).upper() not in {"CHECK", "UNIQUE", "PRIMARY", "FOREIGN", "CONSTRAINT"}:
                    documented.add(col.group(1))
            actual = {
                r[0] for r in rows(
                    conn,
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s",
                    (table,),
                )
            }
            missing = documented - actual
            if missing:
                problems.append(f"{table}: documented but absent in DB: {sorted(missing)}")
        assert problems == [], problems

    def test_no_forbidden_ddl_in_migration(self):
        """HNSW and the optional FTS index must not appear in executed DDL.

        Checks the migration's actual statement lists rather than grepping the
        file, so prose explaining *why* they are absent cannot trip the test.
        """
        import importlib.util

        migration = next((ROOT / "migrations" / "versions").glob("*_initial_schema.py"))
        spec = importlib.util.spec_from_file_location("initial_schema", migration)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        executed = "\n".join(
            module.EXTENSIONS + module.TABLES + module.COMPOSITE_FKS
            + module.INDEXES + module.VIEWS
        ).lower()
        assert "hnsw" not in executed, "HNSW index must not be created yet"

        # The view legitimately *selects* chunks.search_vector; what must not
        # exist is an index built on it.
        indexes = "\n".join(module.INDEXES).lower()
        assert "search_vector" not in indexes, (
            "optional FTS index must not be created by default"
        )
