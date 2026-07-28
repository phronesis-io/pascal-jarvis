#!/usr/bin/env bash
# Pre-hook: claim due user-authored Routines and emit their gathered evidence.
#
# All the work is in core.routines.emit_due_block:
#   1. sweep runs whose process died (a permanent `running` row would make the
#      audit view claim work is in flight that nothing is doing);
#   2. atomically claim every due routine, advancing its next_fire_at watermark
#      so a crash between claim and delivery cannot re-fire it forever;
#   3. run each routine's declared read-only evidence providers;
#   4. record the in-flight run ids for the post-hook.
#
# Empty output = nothing due, and the heartbeat skips the task entirely.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$JARVIS_DIR${PYTHONPATH:+:$PYTHONPATH}"

python3 -m core.routines due 2>/dev/null || true
