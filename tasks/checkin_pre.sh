#!/usr/bin/env bash
# Pre-hook: check if user has free time, inject recent check-in history + mode hint.
# - Only triggers during waking hours (9:00-22:00)
# - Reads last ~5 check-ins from memory/checkin_log.jsonl so Claude can avoid repetition
# - Rotates through different "modes" (mood / curiosity / reflection / etc.) by hour

hour=$(date +%H)
if [ "$hour" -lt 9 ] || [ "$hour" -ge 22 ]; then
  exit 0
fi

now_ts=$(date '+%H:%M')
day=$(date '+%A')
date_ymd=$(date '+%Y-%m-%d')

# Time-of-day flavor — rough buckets
if [ "$hour" -lt 12 ]; then
  phase="morning"
elif [ "$hour" -lt 14 ]; then
  phase="midday"
elif [ "$hour" -lt 18 ]; then
  phase="afternoon"
elif [ "$hour" -lt 20 ]; then
  phase="early-evening"
else
  phase="late-evening"
fi

# Rotate mode by hour-of-day so different calls get different flavors.
# Claude can still ignore/blend this — it's a nudge, not a lock.
MODES=("mood-energy" "curiosity-prompt" "reflection" "micro-challenge" "playful" "sensory-notice" "knowledge-nugget" "callback")
idx=$(( hour % ${#MODES[@]} ))
mode="${MODES[$idx]}"

# Lark calendar — skip if busy in current window
# Uses the Lark plugin's helper when available; no-op otherwise.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
lark_plugin="$JARVIS_DIR/plugins/lark/client.sh"
if [ -f "$lark_plugin" ] && command -v lark-cli &>/dev/null; then
  # shellcheck source=../plugins/lark/client.sh
  . "$lark_plugin"
  start_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  end_iso="$(date -v+1H -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -d '+1 hour' -u +%Y-%m-%dT%H:%M:%SZ)"
  freebusy=$(lark_freebusy "$start_iso" "$end_iso")

  is_free=$(echo "$freebusy" | python3 -c "
import sys, json
from datetime import datetime, timezone
try:
    data = json.load(sys.stdin)
    items = data.get('data') or []
    now = datetime.now(timezone.utc)
    for item in items:
        start = datetime.fromisoformat(item['start_time'])
        end = datetime.fromisoformat(item['end_time'])
        if start <= now < end:
            print('busy')
            sys.exit(0)
except Exception:
    pass
print('free')
" 2>/dev/null || echo "free")

  if [ "$is_free" = "busy" ]; then
    exit 0
  fi
fi

# Last 5 check-ins (if any) — read via Python since JSONL needs parsing
log_file="${MEMORY_DIR:-$HOME/.jarvis/memory}/checkin_log.jsonl"
recent_checkins=""
if [ -f "$log_file" ]; then
  recent_checkins=$(LOG_FILE="$log_file" python3 -c "
import json, os
entries = []
path = os.environ['LOG_FILE']
try:
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
except OSError:
    pass
for e in entries[-5:]:
    print(f\"[{e.get('ts','')}]\")
    print(e.get('content','').strip())
    print()
" 2>/dev/null || true)
fi

cat <<EOF
Current time: $now_ts ($day, $date_ymd) — $phase
Suggested mode this round: $mode

Past check-ins (avoid repeating the same openers, questions, or activity sets):
$recent_checkins
EOF
