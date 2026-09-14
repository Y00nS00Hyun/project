#!/usr/bin/env bash
#
# One database backup, for the timer to run.
#
# Deliberately thin. Everything that decides *what a backup is* -- pg_dump's
# custom format, the .part file, the pg_restore --list verification, the
# .suspect rename, BACKUP_KEEP retention -- lives in scripts/db-backup.sh and
# was verified there, including a restore rehearsal. Reimplementing any of it
# here would mean two backup procedures that could drift apart, and the one
# running unattended at 03:00 would be the one nobody ever watches.
#
# This adds exactly two things: a lock, and a working directory.

set -uo pipefail

# The compose project directory. One path to change if the deployment moves.
PROJECT_DIR="${DOCSEARCH_PROJECT_DIR:-/home/sh-test/project}"

# Held for the whole run. db-backup.sh has no lock of its own, and two dumps at
# once would compete for the same output directory and retention pass -- one
# could prune a file the other is still writing a sibling of.
LOCK_FILE="${DOCSEARCH_BACKUP_LOCK:-/var/lock/docsearch-backup.lock}"

# Where dumps go. Empty means db-backup.sh's own default, which is ./backups
# inside the project. Set DOCSEARCH_BACKUP_DIR in /etc/docsearch/backup.env to
# put them elsewhere on the same machine.
OUT_DIR="${DOCSEARCH_BACKUP_DIR:-}"

exec 9>"$LOCK_FILE" || {
    echo "cannot open lock file $LOCK_FILE" >&2
    exit 1
}

if ! flock --nonblock 9; then
    # Same policy as the ingestion timer: not an error. A backup is already in
    # progress -- run by hand, or a previous tick that has not finished -- so
    # the work this tick wanted done is being done. Exiting 0 keeps the unit
    # out of a failed state for something working as intended.
    echo "a backup is already running; skipping this tick"
    exit 0
fi

cd "$PROJECT_DIR" || {
    echo "compose project directory not found: $PROJECT_DIR" >&2
    exit 1
}

# db-backup.sh reads credentials from .env itself and runs pg_dump inside the
# postgres container, so nothing secret is passed on a command line or set in
# the unit file.
if [[ -n "$OUT_DIR" ]]; then
    exec scripts/db-backup.sh "$OUT_DIR"
fi
exec scripts/db-backup.sh
