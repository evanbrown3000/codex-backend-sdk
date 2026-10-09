#!/usr/bin/env bash
set -euo pipefail

# EvanPC's authenticated ChatGPT profile and singular D1/Drive writer must
# already be present. This installs only read-only export discovery and bounded
# ZIP-range-to-D1 ingestion. It never sends a ChatGPT prompt.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
runtime=/home/evan/.local/share/cognilode/b4pt0r-chatmode
"$runtime/venv/bin/python" -m pip install -r "$here/chatmode-export-requirements.txt"
for name in cognilode-chatmode-export-url.service cognilode-chatmode-export-url.timer \
            cognilode-chatmode-export-backfill.service cognilode-chatmode-export-backfill.timer \
            cognilode-chatmode-export-request.service cognilode-chatmode-export-request.timer; do
    systemctl --user link --force "$here/$name"
done
systemctl --user daemon-reload
systemctl --user enable --now cognilode-chatmode-export-url.timer \
    cognilode-chatmode-export-backfill.timer cognilode-chatmode-export-request.timer
systemctl --user show cognilode-chatmode-export-url.timer \
    cognilode-chatmode-export-backfill.timer cognilode-chatmode-export-request.timer \
    -p ActiveState -p NextElapseUSecRealtime
