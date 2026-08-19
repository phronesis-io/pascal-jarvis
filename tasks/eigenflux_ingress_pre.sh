#!/bin/bash
# Deterministic EigenFlux PM safety net. The WebSocket remains the instant
# path; this poll/cache pass provides proof of reachability and no-loss repair.
set -uo pipefail

ROOT="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || exit 1
exec python3 -m core.eigenflux_ingress
