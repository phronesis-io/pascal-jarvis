#!/usr/bin/env bash
# 缴回制度: sweep pending memorials to a terminal state; emit the morning docket.
# Tier-0 — the Python does all the work and delivers its own card. Empty stdout
# is the correct outcome: 留中 is bookkeeping, and the docket is not prose.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 0
python3 -m tasks.memorial_escrow || true
exit 0
