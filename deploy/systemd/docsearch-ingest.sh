#!/usr/bin/env bash
#
# One ingestion pass: discover new and changed files, parse them, embed them.
#
# Run by docsearch-ingest.service on a timer, and safe to run by hand at any
# time -- including while the timer's own run is in progress.
#
# Everything this does is already idempotent. `sync` skips files whose content
# hash is unchanged, `uq_jobs_active` refuses a second job for a revision that
# already has one, and both claim queries use FOR UPDATE SKIP LOCKED so two
# workers never take the same job. The lock below is not what makes concurrent
# runs safe; it is what makes them not happen, so that a slow run cannot pile
# up behind itself and so the logs read as one pass at a time.

set -uo pipefail

# The compose project directory. Everything else is found relative to it, so
# there is one path to change if the deployment moves.
PROJECT_DIR="${DOCSEARCH_PROJECT_DIR:-/home/sh-test/project}"

# Held for the whole run. A second invocation exits immediately rather than
# waiting, because by the time it could acquire the lock its work has already
# been done by the run that held it.
LOCK_FILE="${DOCSEARCH_INGEST_LOCK:-/var/lock/docsearch-ingest.lock}"

# How many jobs one pass will take. Bounds a single run rather than the queue:
# whatever is left is picked up by the next tick a minute later, so a large
# first import makes several short passes instead of one very long one.
LIMIT="${DOCSEARCH_INGEST_LIMIT:-100}"

exec 9>"$LOCK_FILE" || {
    echo "cannot open lock file $LOCK_FILE" >&2
    exit 1
}

if ! flock --nonblock 9; then
    # Not an error. The previous pass is still going, and this tick has nothing
    # to add. Exiting 0 keeps the unit out of a failed state for something that
    # is working as intended.
    echo "previous ingestion run still in progress; skipping this tick"
    exit 0
fi

cd "$PROJECT_DIR" || {
    echo "compose project directory not found: $PROJECT_DIR" >&2
    exit 1
}

# -T because there is no terminal attached under systemd.
#
# `exec` rather than `run`: the backend container is already up with the shared
# folder mounted and the model cache warm. `run` would start a second container
# and load the embedding model again on every tick.
exec docker compose exec -T backend python -m ingestion run --limit "$LIMIT"
