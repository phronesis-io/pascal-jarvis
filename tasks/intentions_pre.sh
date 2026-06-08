#!/usr/bin/env bash
# intentions_pre.sh — Check for due intents and format for Claude.
#
# Output: JSON with due intents, or empty if nothing due.
# Called by heartbeat runner as the pre-script for intention-check task.

set -euo pipefail
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

JARVIS_DIR="$JARVIS_DIR" MEMORY_DIR="$MEMORY_DIR" python3 - <<'PYTHON'
import os, sys, json
sys.path.insert(0, os.environ['JARVIS_DIR'])

from core.intentions import (
    get_due_intents, mark_triggered, generate_calendar_intents,
    reset_stale_triggered, snapshot_active_intents,
)
from pathlib import Path

# 0. Recover any intents stuck in 'triggered' for >10 min (previous cycle crashed)
try:
    reset_stale_triggered(stale_minutes=10)
except Exception as e:
    print(f"[intentions] Stale-triggered reset error: {e}", file=sys.stderr)

# 1. Auto-generate calendar prep intents (idempotent — skips existing)
cal_file = Path(os.environ['MEMORY_DIR']) / "hot" / "calendar_today.md"
if cal_file.exists():
    try:
        generate_calendar_intents(cal_file.read_text())
    except Exception as e:
        print(f"[intentions] Calendar bridge error: {e}", file=sys.stderr)

# 1b. Refresh the always-on snapshot so EVERY reasoning cycle (main convo +
#     heartbeat) sees the full set of active intents, not just due ones.
#     Must run BEFORE the no-due early-exit below.
try:
    snapshot_active_intents(os.environ['MEMORY_DIR'])
except Exception as e:
    print(f"[intentions] Snapshot error: {e}", file=sys.stderr)

# 2. Check for due intents
due = get_due_intents()
if not due:
    sys.exit(0)  # Empty output → heartbeat skips

# 3. Mark as triggered (prevents re-pickup next cycle)
for intent in due:
    mark_triggered(intent["id"])

# 4. Format for Claude
output = {
    "count": len(due),
    "intents": []
}
for intent in due:
    ctx = json.loads(intent["context"]) if isinstance(intent["context"], str) else intent["context"]
    tags = json.loads(intent["tags"]) if isinstance(intent["tags"], str) else intent["tags"]
    output["intents"].append({
        "id": intent["id"],
        "name": intent["name"],
        "purpose": intent["purpose"],
        "prompt": intent["prompt"],
        "context": ctx,
        "action_type": intent["action_type"],
        "action_config": json.loads(intent["action_config"]) if isinstance(intent["action_config"], str) else intent["action_config"],
        "tags": tags,
        "source": intent["source"],
    })

print(json.dumps(output, ensure_ascii=False))
PYTHON
