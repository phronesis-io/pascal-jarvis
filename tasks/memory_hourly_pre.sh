#!/usr/bin/env bash
# Pre-hook: extract last hour's conversation from active sessions.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SESSION_DIR="${SESSION_DIR:-$HOME/.claude/projects}"
TRACKER="$JARVIS_DIR/active_sessions.json"

[ -f "$TRACKER" ] || exit 0

export JARVIS_DIR SESSION_DIR TRACKER

python3 - <<'PYEOF'
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

tracker_path = os.environ["TRACKER"]
sdir = Path(os.environ["SESSION_DIR"])
NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

tracker = json.load(open(tracker_path))
cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")

all_msgs = []

for conv_key, entry in tracker.items():
    sid = entry.get("session_id", "")
    counter = entry.get("counter", 0)
    if not sid:
        continue
    candidates = [sid]
    if counter > 1:
        prev = str(uuid.uuid5(NAMESPACE, f"{conv_key}-{counter - 1}"))
        candidates.append(prev)

    for cand in candidates:
        path = sdir / f"{cand}.jsonl"
        if not path.exists():
            continue
        for line in path.open(encoding="utf-8", errors="ignore"):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            ts = obj.get("timestamp", "")
            if ts < cutoff:
                continue
            msg = obj.get("message", {})
            role = msg.get("role", obj.get("type", ""))
            content = msg.get("content", "")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        parts.append(c.get("text", ""))
                text = "\n".join(parts)
            text = text.strip()
            if not text:
                continue
            all_msgs.append({"role": role, "text": text[:500], "ts": ts[:16]})

if not all_msgs:
    raise SystemExit(0)

all_msgs.sort(key=lambda m: m["ts"])
print(f"Messages from last hour ({len(all_msgs)} messages):")
for m in all_msgs:
    print(f"[{m['ts']}] {m['role']}: {m['text'][:200]}")
PYEOF
