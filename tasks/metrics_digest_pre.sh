#!/usr/bin/env bash
# Pre-hook for the `metrics-digest` heartbeat task — thin wrapper around
# core/metrics_digest.py (records newer than the watermark + absence alerts;
# see that module's docstring). Empty stdout = task skipped at zero cost.
set -uo pipefail
export LC_ALL=C

# cd to the REPO for module resolution; JARVIS_DIR (the data root) may be a
# different directory, e.g. in tests.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
JARVIS_DIR="${JARVIS_DIR:-$REPO_DIR}"
cd "$REPO_DIR" || exit 0

JARVIS_DIR="$JARVIS_DIR" python3 -m core.metrics_digest 2>&1
