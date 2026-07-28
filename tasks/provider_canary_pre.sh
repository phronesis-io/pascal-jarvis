#!/bin/bash
# Verify each configured model route with a tiny, bounded request.
#
# `core.provider_health probe` exits 1 when any rung is unhealthy — the right
# CLI contract for a human or a script asking "is the chain OK?". It is the
# WRONG signal for the heartbeat, which reads a nonzero pre-script as "this
# task failed" and trips the task's circuit breaker.
#
# Those are different claims. The canary finding an unhealthy provider is the
# canary SUCCEEDING: that finding is its entire product. Conflating them meant
# a real backup-relay outage (HTTP 402, no balance) opened the canary's own
# circuit, which then skipped ~3.8k times in one day and stopped re-probing —
# the monitor going dark exactly when there was something to monitor, and one
# more "health check that lies" (the class core/brain_health.py exists to stop).
#
# So: exit 0 whenever the probe RAN and produced a provider report. Exit
# nonzero only when the probe itself could not run — a crash, no output, or
# unparseable output. The unhealthy rung is still reported honestly through
# data/provider_health.json and the Ops provider panel that reads it.
set -uo pipefail

ROOT="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || exit 1

report=$(python3 -m core.provider_health probe 2>/dev/null)
probe_rc=$?

if [ -n "$report" ] && printf '%s' "$report" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    sys.exit(1)
sys.exit(0 if payload.get("providers") else 1)
' 2>/dev/null; then
  printf '%s\n' "$report"
  exit 0
fi

# The probe genuinely could not run — that IS a task failure.
echo "provider canary could not complete (exit ${probe_rc})" >&2
exit 1
