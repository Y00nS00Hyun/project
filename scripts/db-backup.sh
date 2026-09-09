#!/usr/bin/env bash
#
# PostgreSQL logical backup for the compose deployment.
#
#   scripts/db-backup.sh                    # -> backups/docsearch-<timestamp>.dump
#   scripts/db-backup.sh /mnt/nas/backups   # write somewhere else
#
# Uses pg_dump's custom format (-Fc): compressed, and restorable selectively
# with pg_restore. A plain SQL file would also work but cannot be restored
# table-by-table, which is what you want when only one table is damaged.
#
# The dump is taken inside the postgres container, so no client tooling is
# needed on the host and the credentials never appear in a host process list.

set -euo pipefail

cd "$(dirname "$0")/.."

OUT_DIR="${1:-backups}"
KEEP="${BACKUP_KEEP:-7}"

# Credentials come from .env, the same file compose uses. Never passed on the
# command line, where `ps` would show them.
if [[ ! -f .env ]]; then
    echo "error: .env not found. Copy .env.example and fill it in." >&2
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

mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$OUT_DIR/${POSTGRES_DB}-${STAMP}.dump"

echo "backing up ${POSTGRES_DB} -> ${TARGET}"

# Write to a .part file first: a backup interrupted halfway must not be left
# sitting there looking like a usable one.
docker compose exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges \
    > "${TARGET}.part"
mv "${TARGET}.part" "$TARGET"

SIZE="$(du -h "$TARGET" | cut -f1)"
echo "  wrote ${SIZE}"

# Verify the dump is readable before reporting success. pg_restore --list
# parses the archive's table of contents, so a truncated or corrupt file fails
# here rather than during an emergency restore.
ENTRIES="$(docker compose exec -T postgres pg_restore --list < "$TARGET" | grep -c '^[0-9]' || true)"
if [[ "$ENTRIES" -lt 1 ]]; then
    echo "error: dump is not readable by pg_restore; keeping it as ${TARGET}.suspect" >&2
    mv "$TARGET" "${TARGET}.suspect"
    exit 1
fi
echo "  verified: ${ENTRIES} archive entries"

# Retention. Only files this script produced are considered.
if [[ "$KEEP" -gt 0 ]]; then
    mapfile -t OLD < <(ls -1t "$OUT_DIR"/${POSTGRES_DB}-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))")
    for f in "${OLD[@]:-}"; do
        [[ -n "$f" ]] || continue
        echo "  pruning $(basename "$f")"
        rm -f "$f"
    done
fi

echo "OK ${TARGET}"
