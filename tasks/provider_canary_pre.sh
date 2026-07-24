#!/bin/bash
# Verify each configured model route with a tiny, bounded request.
set -uo pipefail

ROOT="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || exit 1
exec python3 -m core.provider_health probe
