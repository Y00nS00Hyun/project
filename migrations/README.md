# Database Migrations

Alembic migrations for the internal document management system.
The authoritative schema is [`docs/database-schema-v2.5.md`](../docs/database-schema-v2.5.md);
these scripts transcribe it.

## Requirements

```text
PostgreSQL 13+      (developed and tested on 16.2)
pgvector            chunks.embedding VECTOR(384)
pg_trgm             lexical search route
pgcrypto            declared by the schema document
```

Extensions are created by the migration. If one is unavailable the migration
**fails loudly** — there is no fallback and no alternative engine.

## Running

The database URL comes from the environment, never from `alembic.ini`, so
credentials stay out of the repository.

```bash
export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname

alembic upgrade head      # apply
alembic current           # show applied revision
alembic downgrade base    # roll back to empty
alembic history           # list revisions
```

## What the initial migration creates

```text
extensions   vector, pgcrypto, pg_trgm
tables       16 application tables
FKs          26, including 2 composite FKs on documents -> document_revisions
indexes      27 named indexes (plus PK/UNIQUE backing indexes)
view         current_ready_chunks
```

### Deliberately absent

| Object | Why |
| --- | --- |
| HNSW index on `chunks.embedding` | Initial search is exact cosine (Design Freeze v1). Add after measuring chunk count and latency |
| GIN index on `chunks.search_vector` | Optional FTS extension only; the pg_trgm route does not use it |
| `DROP EXTENSION` in `downgrade()` | Extensions are database-wide. Dropping `vector` would invalidate any other schema using the type |

## Adding a revision

`--autogenerate` is **not** usable: there is no ORM metadata
(`target_metadata = None`), and autogenerate does not reliably reproduce CHECK
constraints, composite FKs or generated columns. Write the DDL explicitly.

```bash
alembic revision -m "add something"
```

Then update `docs/database-schema-v2.x.md` in the same change.
`tests/test_migrations.py::TestSchemaDrift` fails if the document and the
database disagree on tables, columns, indexes or views.

## Tests

```bash
pytest tests/test_migrations.py -q
```

Runs against a real PostgreSQL started by `pgserver` (pip-installed,
self-contained — no Docker, no root). SQLite is never substituted: CHECK
constraints, composite FKs, generated columns and the `vector` type do not
exist there, so a green run on SQLite would prove nothing.
