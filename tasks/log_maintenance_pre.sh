#!/bin/bash
# Rotate launchd-owned logs only after their writers have closed descriptors.
set -uo pipefail

ROOT="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || exit 1
exec python3 -m core.log_maintenance
