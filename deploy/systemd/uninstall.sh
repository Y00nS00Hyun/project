#!/usr/bin/env bash
#
# Stop and remove the ingestion timer. Leaves /etc/docsearch/ingest.env alone:
# it is the operator's file, and removing a schedule is not a reason to discard
# their settings.
#
#     sudo ./deploy/systemd/uninstall.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo" >&2
    exit 1
fi

systemctl disable --now docsearch-ingest.timer 2>/dev/null || true
rm -f /etc/systemd/system/docsearch-ingest.timer /etc/systemd/system/docsearch-ingest.service
systemctl daemon-reload
echo "removed. ingestion is manual again:"
echo "  docker compose exec backend python -m ingestion run"
