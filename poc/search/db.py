"""Throwaway PostgreSQL instance for the search PoC.

This is **not** a production schema and not a migration. It is the smallest
table set that lets simple FTS, pg_trgm and pgvector be compared on the same
data. The real schema lives in docs/database-schema-v2.3.md and is not touched
by this PoC.

The server is a pip-installed, self-contained PostgreSQL (``pgserver``) with a
data directory under the PoC's own working area, so nothing on the machine is
modified.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from dataset import Chunk, Document

#: Embedding dimension of the PoC model. NOT a production decision.
EMBEDDING_DIM = 384

SCHEMA = f"""
DROP TABLE IF EXISTS poc_chunks;
DROP TABLE IF EXISTS poc_documents;

CREATE TABLE poc_documents (
    document_id   TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    department    TEXT NOT NULL,
    year          INT  NOT NULL,
    document_type TEXT NOT NULL,
    body          TEXT NOT NULL,
    content       TEXT NOT NULL,
    search_vector TSVECTOR
);

CREATE TABLE poc_chunks (
    chunk_id     SERIAL PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES poc_documents(document_id) ON DELETE CASCADE,
    chunk_index  INT  NOT NULL,
    text         TEXT NOT NULL,
    embedding    VECTOR({EMBEDDING_DIM}),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX idx_poc_documents_fts ON poc_documents USING GIN(search_vector);
CREATE INDEX idx_poc_documents_trgm ON poc_documents USING GIN(content gin_trgm_ops);
"""


def start_server(data_dir: Path):
    """Start (or reuse) the embedded PostgreSQL and ensure extensions exist."""
    import pgserver

    data_dir = Path(data_dir)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(data_dir, cleanup_mode=None)
    for extension in ("vector", "pg_trgm"):
        server.psql(f"CREATE EXTENSION IF NOT EXISTS {extension}")
    return server


def connect(uri: str) -> psycopg.Connection:
    return psycopg.connect(uri, autocommit=True)


def load_corpus(
    conn: psycopg.Connection,
    documents: list[Document],
    chunks: list[Chunk],
    embeddings: dict[tuple[str, int], list[float]] | None = None,
) -> None:
    """Rebuild the PoC tables from scratch and load the dataset."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        for d in documents:
            cur.execute(
                """
                INSERT INTO poc_documents
                    (document_id, title, department, year, document_type, body, content, search_vector)
                VALUES (%s, %s, %s, %s, %s, %s, %s, to_tsvector('simple', %s))
                """,
                (d.document_id, d.title, d.department, d.year, d.document_type,
                 d.text, d.searchable_content, d.searchable_content),
            )
        for c in chunks:
            vector = None
            if embeddings is not None:
                vector = embeddings.get((c.document_id, c.chunk_index))
            cur.execute(
                """
                INSERT INTO poc_chunks (document_id, chunk_index, text, embedding)
                VALUES (%s, %s, %s, %s)
                """,
                (c.document_id, c.chunk_index, c.text,
                 str(vector) if vector is not None else None),
            )


def default_data_dir() -> Path:
    """Where the throwaway cluster lives.

    Overridable with SEARCH_POC_PGDATA so a CI or internal run can place it on
    a suitable volume.
    """
    override = os.environ.get("SEARCH_POC_PGDATA")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / ".search-poc-pgdata"
