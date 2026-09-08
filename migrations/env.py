"""Alembic environment.

The schema is defined as explicit SQL in the version scripts rather than
reflected from SQLAlchemy models. The authoritative source is
``docs/database-schema-v2.5.md``; the migration transcribes it.

There is no ORM model layer yet, so ``target_metadata`` is None and
``--autogenerate`` is intentionally not usable. When the ORM is added later,
autogenerate must be reviewed against the schema document rather than trusted
blindly (it does not reproduce CHECK constraints or composite FKs reliably).
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM metadata yet -- see module docstring.
target_metadata = None


def get_url() -> str:
    """Read the database URL from the environment.

    Fails loudly rather than falling back to a default, so a migration can
    never be applied to an unintended database.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        print(
            "DATABASE_URL is not set.\n"
            "  export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
