#!/bin/bash
# Deterministic Tier-0 reconciliation. User attention is routed by the
# Delegation projector itself; stdout is only an operational audit record.
set -uo pipefail

ROOT="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || exit 1
exec python3 -m core.delegation_reconcile --limit 50
