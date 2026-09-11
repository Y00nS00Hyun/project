#!/usr/bin/env bash
#
# Install the ingestion timer. Run once, with sudo, on the VM.
#
#     sudo ./deploy/systemd/install.sh
#
# Idempotent: re-run it after editing the unit files to pick up the changes.
# To remove everything again, see uninstall.sh.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$HERE/../.." && pwd)"
UNIT_DIR=/etc/systemd/system
CONFIG_DIR=/etc/docsearch

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo -- this writes to $UNIT_DIR" >&2
    exit 1
fi

# The units name the deployment's owner and directory. Rather than asking an
# operator to keep three files in sync by hand, they are substituted from the
# checkout being installed.
OWNER="$(stat -c '%U' "$PROJECT_DIR")"

install -d -m 0755 "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/ingest.env" ]]; then
    # Not overwritten on re-install: it is the operator's file once it exists.
    cat > "$CONFIG_DIR/ingest.env" <<ENV
# Settings for the ingestion timer. Optional -- the wrapper has defaults for
# all of these.
#
# The schedule itself is NOT here: it is a systemd directive, and lives in
# docsearch-ingest.timer. Change it with
#     sudo systemctl edit docsearch-ingest.timer
#     sudo systemctl restart docsearch-ingest.timer

# Jobs per pass. Bounds one run, not the queue -- whatever is left is taken by
# the next tick.
DOCSEARCH_INGEST_LIMIT=100
ENV
    echo "wrote $CONFIG_DIR/ingest.env"
fi

for unit in docsearch-ingest.service docsearch-ingest.timer; do
    sed -e "s|/home/sh-test/project|$PROJECT_DIR|g" \
        -e "s|^User=.*|User=$OWNER|" \
        "$HERE/$unit" > "$UNIT_DIR/$unit"
    chmod 0644 "$UNIT_DIR/$unit"
    echo "installed $UNIT_DIR/$unit"
done

chmod 0755 "$HERE/docsearch-ingest.sh"

systemctl daemon-reload
# enable --now: starts the timer and survives reboot. The service itself is
# never enabled -- it has no [Install] section and is only ever started by the
# timer or by hand.
systemctl enable --now docsearch-ingest.timer

echo
systemctl list-timers docsearch-ingest.timer --no-pager
echo
echo "Logs:            journalctl -u docsearch-ingest.service -f"
echo "Run now:         sudo systemctl start docsearch-ingest.service"
echo "Change interval: sudo systemctl edit docsearch-ingest.timer"
