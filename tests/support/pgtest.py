"""Ephemeral PostgreSQL for migration tests.

Uses ``pgserver`` (a pip-installed, self-contained PostgreSQL) so the suite
needs no system PostgreSQL, no Docker and no root. It is a *real* PostgreSQL --
the migration is never validated against SQLite or a mock, because CHECK
constraints, composite FKs, generated columns and pgvector types do not exist
there.

If the required extensions are missing the tests fail loudly rather than
skipping silently; a green suite must mean the schema really was created.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

REQUIRED_EXTENSIONS = ("vector", "pg_trgm", "pgcrypto")


def data_dir() -> Path:
    """Cluster location. Override with SEARCH_POC_PGDATA / MIGRATION_TEST_PGDATA."""
    override = os.environ.get("MIGRATION_TEST_PGDATA") or os.environ.get("SEARCH_POC_PGDATA")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / ".migration-test-pgdata"


def start_server():
    import pgserver

    directory = data_dir()
    directory.parent.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(directory, cleanup_mode=None)


def _socket_uri(server, dbname: str, driver: str) -> str:
    """Build a URI for ``dbname`` on the same cluster.

    pgserver connects over a unix socket, so the host is a directory path and
    must be passed as a query parameter rather than a netloc.
    """
    base = server.get_uri()
    host = base.split("host=", 1)[1]
    return f"{driver}://postgres@/{dbname}?host={quote(host, safe='/')}"


def create_database(server, dbname: str) -> str:
    """(Re)create an empty database and return its SQLAlchemy URL."""
    import psycopg

    admin = _socket_uri(server, "postgres", "postgresql")
    with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        cur.execute(f'CREATE DATABASE "{dbname}"')
    return _socket_uri(server, dbname, "postgresql+psycopg")


def psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def check_extensions_available(server) -> list[str]:
    """Return the required extensions this cluster cannot provide."""
    import psycopg

    uri = _socket_uri(server, "postgres", "postgresql")
    with psycopg.connect(uri, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM pg_available_extensions")
        available = {r[0] for r in cur.fetchall()}
    return [e for e in REQUIRED_EXTENSIONS if e not in available]
