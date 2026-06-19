#!/usr/bin/env bash
# intentions_pre.sh — Check for due intents and format for Claude.
#
# Output: JSON with due intents (+ any breach notifications owed), or empty
# if nothing due. Called by heartbeat runner as the pre-script for the
# intention-check task.
#
# v2 (REQ-30/31/32): after mark_triggered, the due ids are persisted to the
# inflight manifest (data/.intention_inflight.json) so intentions_post.py can
# deterministically reconcile what Claude's envelope did NOT cover — the
# execution-ack no longer depends on a perfect LLM JSON round-trip. The
# breach queue carries commitments that exhausted their retries; they ride
# this cycle as an apology card so dropped reminders are never silent.

set -euo pipefail
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

JARVIS_DIR="$JARVIS_DIR" MEMORY_DIR="$MEMORY_DIR" python3 - <<'PYTHON'
import os, sys, json
sys.path.insert(0, os.environ['JARVIS_DIR'])

from core.intentions import (
    get_due_intents, mark_triggered, generate_calendar_intents,
    lifecycle_sweep, snapshot_active_intents, write_inflight, peek_breaches,
    generate_closure_reask_intents,
)
from pathlib import Path

# 0. Lifecycle sweep: retry stuck intents (bounded), expire exhausted ones
#    with a breach notification, retire awaiting-closure zombies (TTL).
try:
    lifecycle_sweep(stale_minutes=10)
except Exception as e:
    print(f"[intentions] lifecycle sweep error: {e}", file=sys.stderr)

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

# 1c. Re-surface drained hard/external closures as normal bounded intents.
#     This consumes get_closure_due() in the heartbeat path: a follow-up that
#     asked once and got no answer must not silently sink forever.
try:
    reasks = generate_closure_reask_intents(limit=3)
    if reasks:
        print(f"[intentions] closure reasks generated: {len(reasks)}", file=sys.stderr)
except Exception as e:
    print(f"[intentions] closure reask error: {e}", file=sys.stderr)

# 2. Check for due intents + breach notifications owed. peek_breaches is
#    NON-mutating (red-team fix): the notify_attempts counter is bumped by
#    intentions_post ONLY when a card actually renders, never per pre-run.
try:
    due = get_due_intents()
except Exception as e:
    # A DB-lock / transient error in the due-check (incl. cleanup_expired and
    # the REQ-60 stale-closure write) must not strand the cycle — degrade to
    # no-due this tick rather than crash the pre-script (red-team fix).
    print(f"[intentions] get_due_intents error: {e}", file=sys.stderr)
    due = []
try:
    breaches = peek_breaches()
except Exception as e:
    print(f"[intentions] breach peek error: {e}", file=sys.stderr)
    breaches = []

if not due and not breaches:
    sys.exit(0)  # Empty output → heartbeat skips

# 3. Mark as triggered (prevents re-pickup next cycle) and persist the
#    inflight manifest — the deterministic execution-ack (REQ-30). The breach
#    ids riding THIS cycle's prompt are recorded so post marks exactly those
#    shown (not a blanket wipe that would eat reconcile's fresh breaches).
for intent in due:
    mark_triggered(intent["id"])
try:
    write_inflight([i["id"] for i in due], breach_ids=[b.get("id", "") for b in breaches])
except Exception as e:
    print(f"[intentions] inflight manifest write error: {e}", file=sys.stderr)

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
        # ── closure model — INPUT/DECISION must reach Claude, else 3 件套缺 2 ──
        "category": intent.get("category", "none"),
        "input_ctx": intent.get("input_ctx", ""),
        "decision": intent.get("decision", ""),
        "closure_question": intent.get("closure_question", ""),
        "parent_intent_id": intent.get("parent_intent_id"),
    })

# 5. Breach notifications: commitments whose reminders were dropped after
#    retries. Claude must surface ONE apology card covering them — the
#    original prompt rides along so the reminder's value isn't lost.
if breaches:
    output["breaches"] = [{
        "id": b.get("id", ""),
        "name": b.get("name", ""),
        "original_prompt": b.get("prompt", ""),
        "purpose": b.get("purpose", ""),
        "was_due_at": b.get("trigger_time", ""),
        "attempts": b.get("attempt", 0),
    } for b in breaches]

print(json.dumps(output, ensure_ascii=False))
PYTHON
