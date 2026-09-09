#!/usr/bin/env bash
#
# Restore a dump produced by scripts/db-backup.sh.
#
#   scripts/db-restore.sh backups/docsearch-20260909-071500.dump
#       -> restores into a SEPARATE database (docsearch_restore_test) and
#          reports row counts. The live database is not touched.
#
#   scripts/db-restore.sh <dump> --into-production
#       -> replaces the live database. Requires typing the database name to
#          confirm. This is the disaster path, not the rehearsal path.
#
# The default is the rehearsal, deliberately. A restore script whose easiest
# invocation overwrites production is a script that eventually does.

set -euo pipefail

cd "$(dirname "$0")/.."

DUMP="${1:-}"
MODE="${2:-rehearsal}"

if [[ -z "$DUMP" || ! -f "$DUMP" ]]; then
    echo "usage: $0 <dump-file> [--into-production]" >&2
    exit 2
fi

if [[ ! -f .env ]]; then
    echo "error: .env not found." >&2
    exit 2
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

: "${POSTGRES_USER:?POSTGRES_USER is not set in .env}"
: "${POSTGRES_DB:?POSTGRES_DB is not set in .env}"

if ! docker compose ps --status running --services | grep -qx postgres; then
    echo "error: the postgres service is not running." >&2
    exit 1
fi

psql_run() {
    docker compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 "$@"
}

if [[ "$MODE" == "--into-production" ]]; then
    TARGET_DB="$POSTGRES_DB"
    echo "*** This REPLACES the live database '${TARGET_DB}'. ***"
    echo "Type the database name to continue:"
    read -r CONFIRM
    if [[ "$CONFIRM" != "$TARGET_DB" ]]; then
        echo "aborted." >&2
        exit 1
    fi
    # The API holds connections; they must be gone before the database can be
    # dropped. Stopping the app is part of the procedure, not an afterthought.
    echo "stopping backend so it releases its connections"
    docker compose stop backend >/dev/null
else
    TARGET_DB="${POSTGRES_DB}_restore_test"
    echo "rehearsal: restoring into '${TARGET_DB}' (live database untouched)"
fi

echo "recreating ${TARGET_DB}"
psql_run -c "DROP DATABASE IF EXISTS ${TARGET_DB} WITH (FORCE);" >/dev/null
psql_run -c "CREATE DATABASE ${TARGET_DB};" >/dev/null

echo "restoring"
# --no-owner/--no-privileges: the dump is restored as whoever runs this, which
# is what makes it portable to a rebuilt VM with a different role setup.
docker compose exec -T postgres \
    pg_restore -U "$POSTGRES_USER" -d "$TARGET_DB" --no-owner --no-privileges \
    < "$DUMP"

echo
echo "restored contents of ${TARGET_DB}:"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$TARGET_DB" -tAc "
SELECT '  alembic         ' || COALESCE((SELECT version_num FROM alembic_version), 'MISSING')
UNION ALL SELECT '  tables          ' || count(*)::text FROM information_schema.tables
    WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name<>'alembic_version'
UNION ALL SELECT '  views           ' || count(*)::text FROM information_schema.views WHERE table_schema='public'
UNION ALL SELECT '  documents       ' || count(*)::text FROM documents
UNION ALL SELECT '  revisions       ' || count(*)::text FROM document_revisions
UNION ALL SELECT '  chunks          ' || count(*)::text FROM chunks
UNION ALL SELECT '  embedded chunks ' || count(*)::text FROM chunks WHERE embedding IS NOT NULL
UNION ALL SELECT '  users           ' || count(*)::text FROM users
UNION ALL SELECT '  departments     ' || count(*)::text FROM departments
UNION ALL SELECT '  permissions     ' || count(*)::text FROM document_permissions;
"

if [[ "$MODE" == "--into-production" ]]; then
    echo
    echo "restarting backend"
    docker compose up -d backend >/dev/null
    echo "OK restored into ${TARGET_DB} (live)"
else
    echo
    echo "OK rehearsal complete. Drop it when you are done:"
    echo "  docker compose exec -T postgres psql -U ${POSTGRES_USER} -d postgres -c 'DROP DATABASE ${TARGET_DB};'"
fi
