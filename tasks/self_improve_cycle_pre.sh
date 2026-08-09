#!/usr/bin/env bash
# Pre-hook: gate + spawn the detached daily self-improve session.
# ALWAYS prints nothing — the heartbeat model is never involved; this task
# exists only to borrow the scheduler. stderr is kept for the heartbeat log.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$JARVIS_DIR${PYTHONPATH:+:$PYTHONPATH}"

python3 -m core.self_improve_cycle tick || true
exit 0
