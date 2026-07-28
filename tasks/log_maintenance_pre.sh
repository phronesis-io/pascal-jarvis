#!/bin/bash
# Rotate launchd-owned logs only after their writers have closed descriptors.
set -uo pipefail

ROOT="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || exit 1

# Attention ROI governor: recompute which sources still earn a decision lane
# from the ledger's own engagement evidence. Deterministic, Tier-0, and
# fail-open — a governor problem must never block log rotation.
python3 -m core.attention_roi refresh >/dev/null 2>&1 || true

exec python3 -m core.log_maintenance
