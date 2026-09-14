#!/usr/bin/env bash
#
# Stop and remove the ingestion and backup timers. Leaves /etc/docsearch alone:
# those are the operator's files, and removing a schedule is not a reason to
# discard their settings. Existing dumps are left where they are -- removing a
# schedule is not a reason to delete backups either.
#
#     sudo ./deploy/systemd/uninstall.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

systemctl disable --now docsearch-ingest.timer 2>/dev/null || true
systemctl disable --now docsearch-backup.timer 2>/dev/null || true
rm -f /etc/systemd/system/docsearch-ingest.timer /etc/systemd/system/docsearch-ingest.service \
      /etc/systemd/system/docsearch-backup.timer /etc/systemd/system/docsearch-backup.service
systemctl daemon-reload
echo "removed. both are manual again:"
echo "  docker compose exec backend python -m ingestion run"
echo "  scripts/db-backup.sh"
