#!/usr/bin/env bash
# Pre-hook: detect transition moments + inject context for value-driven check-ins.
#
# Design principle (CHI 2025): interrupt at TRANSITIONS (meeting just ended,
# focus block completed), not during idle time. Idle ≠ bored; idle may = thinking.
#
# - Only triggers during waking hours (9:00-22:00)
# - Detects transition context from calendar (meeting just ended? big gap ahead?)
# - Reads last ~5 check-ins to avoid repetition
# - Rotates through value-oriented "modes" by hour

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

# Rotate mode — all value-oriented, no empty greeting modes
MODES=("knowledge-nugget" "curiosity-prompt" "reflection" "micro-challenge" "market-insight" "tech-trend" "philosophy-bite" "callback")
idx=$(( hour % ${#MODES[@]} ))
mode="${MODES[$idx]}"

# ── Calendar context: transition detection + next-event lookahead ──
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
lark_plugin="$JARVIS_DIR/plugins/lark/client.sh"
transition_context=""

if [ -f "$lark_plugin" ] && command -v lark-cli &>/dev/null; then
  # shellcheck source=../plugins/lark/client.sh
  . "$lark_plugin"

  # Look back 1h and forward 2h to detect transitions
  past_iso="$(date -v-1H -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -d '-1 hour' -u +%Y-%m-%dT%H:%M:%SZ)"
  now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  future_iso="$(date -v+2H -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -d '+2 hours' -u +%Y-%m-%dT%H:%M:%SZ)"

  # Get events in the [-1h, +2h] window
  freebusy=$(lark_freebusy "$past_iso" "$future_iso")

  transition_context=$(echo "$freebusy" | python3 -c "
import sys, json
from datetime import datetime, timezone, timedelta

try:
    data = json.load(sys.stdin)
    items = data.get('data') or []
    now = datetime.now(timezone.utc)
    signals = []

    currently_busy = False
    just_ended = None       # meeting that ended in last 15 min
    next_event = None       # next upcoming event
    free_until = None       # how long until next event

    for item in items:
        start = datetime.fromisoformat(item['start_time'])
        end = datetime.fromisoformat(item['end_time'])

        # Currently in a meeting → skip checkin
        if start <= now < end:
            print('BUSY')
            sys.exit(0)

        # Meeting ended in the last 15 min → transition moment!
        if end <= now and (now - end) < timedelta(minutes=15):
            just_ended = item
            signals.append(f'transition: meeting ended {int((now - end).total_seconds() / 60)}m ago')

        # Next upcoming event
        if start > now and (next_event is None or start < datetime.fromisoformat(next_event['start_time'])):
            next_event = item

    if next_event:
        next_start = datetime.fromisoformat(next_event['start_time'])
        gap_min = int((next_start - now).total_seconds() / 60)
        signals.append(f'next_event_in: {gap_min}m')
        if gap_min < 20:
            signals.append('tight_window: true (less than 20m, maybe skip)')
        elif gap_min > 90:
            signals.append(f'large_free_block: {gap_min}m available')
    else:
        signals.append('no_upcoming_events: rest of day is clear')

    if just_ended:
        signals.append('best_moment: post-meeting transition')
    elif next_event and int((datetime.fromisoformat(next_event['start_time']) - now).total_seconds() / 60) < 20:
        # Too close to next meeting — bad time to interrupt
        print('BUSY')
        sys.exit(0)

    print('\\n'.join(signals))
except Exception as e:
    print(f'calendar_error: {e}')
" 2>/dev/null || echo "calendar_unavailable")

  # If calendar says busy, skip
  if [ "$transition_context" = "BUSY" ]; then
    exit 0
  fi
fi

# Last 5 check-ins (if any)
log_file="${MEMORY_DIR:-$HOME/.jarvis/memory}/system/checkin_log.jsonl"
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

# Load interests for context-aware nuggets
interests_file="${MEMORY_DIR:-$HOME/.jarvis/memory}/warm/interests.md"
interests=""
if [ -f "$interests_file" ]; then
  interests=$(cat "$interests_file" 2>/dev/null)
fi

cat <<EOF
Current time: $now_ts ($day, $date_ymd) — $phase
Suggested mode this round: $mode

Calendar context:
$transition_context

User interests (for relevant knowledge nuggets):
$interests

Past check-ins (MUST avoid repeating topics, openers, or structure):
$recent_checkins
EOF
