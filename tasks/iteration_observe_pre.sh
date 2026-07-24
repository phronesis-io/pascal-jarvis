#!/bin/bash
# Daily L3 observe: feedback becomes a reviewable proposal, never an
# automatically executable engineering task.
set -uo pipefail

ROOT="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || exit 1
exec python3 -m core.iteration_loop observe
