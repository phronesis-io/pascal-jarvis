#!/usr/bin/env bash
# Pre-hook: detect approaching or current free blocks and surface them.
# Rate-limited to max 2 nudges per day.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

# Load configurable thresholds from jarvis.yaml
eval $(bash "$JARVIS_DIR/scripts/config_env.sh" 2>/dev/null) || true

# Only during waking hours (configurable via schedule.working_hours)
hour=$(date +%H)
if [ "$hour" -lt "${WORK_START:-9}" ] || [ "$hour" -ge "${WORK_END:-22}" ]; then
  exit 0
fi

# Rate limit (configurable via thresholds.nudge_max_per_day)
STATE_FILE="$JARVIS_DIR/.free_time_nudge_state"
today=$(date '+%Y-%m-%d')
nudge_count=0
if [ -f "$STATE_FILE" ]; then
  state_date=$(head -1 "$STATE_FILE" 2>/dev/null)
  if [ "$state_date" = "$today" ]; then
    nudge_count=$(tail -1 "$STATE_FILE" 2>/dev/null || echo 0)
  fi
fi

if [ "$nudge_count" -ge "${NUDGE_MAX:-2}" ]; then
  exit 0
fi

# ── Compute current free block from calendar ──
calendar_file="$MEMORY_DIR/hot/calendar_today.md"
[ -f "$calendar_file" ] || exit 0

free_block=$(python3 -c "
import re
from datetime import datetime

now = datetime.now()
now_hm = now.strftime('%H:%M')
content = open('$calendar_file').read()
lines = content.split('\n')

# Extract today's events
events = []
in_today = False
for line in lines:
    if re.match(r'.*([Tt]oday|今天)', line):
        in_today = True
        continue
    if in_today:
        if re.match(r'.*(Tomorrow|明天|Day \d|后天|周)', line):
            break
        m = re.match(r'\s*-?\s*(\d{2}:\d{2})-(\d{2}:\d{2})\s+(.*)', line)
        if m:
            events.append((m.group(1), m.group(2), m.group(3).strip()))

events.sort()

# Find current or approaching free block
# A free block is the gap between now and the next event (if > 30min)
next_event = None
for start, end, title in events:
    if start > now_hm:
        next_event = (start, title)
        break

# Check if we're currently in a free block (not during any event)
in_event = False
for start, end, title in events:
    if start <= now_hm < end:
        in_event = True
        break

if in_event:
    # Currently busy
    pass
elif next_event:
    # Free until next event
    try:
        next_t = datetime.strptime(next_event[0], '%H:%M').replace(
            year=now.year, month=now.month, day=now.day)
        gap_min = int((next_t - now).total_seconds() / 60)
        if gap_min >= 30:
            print(f'FREE_BLOCK: now until {next_event[0]} ({gap_min}min), next: {next_event[1]}')
    except:
        pass
else:
    # No more events today
    end_time = '22:00'
    try:
        end_t = datetime.strptime(end_time, '%H:%M').replace(
            year=now.year, month=now.month, day=now.day)
        gap_min = int((end_t - now).total_seconds() / 60)
        if gap_min >= 30:
            print(f'FREE_BLOCK: now until end of day ({gap_min}min), no more events')
    except:
        pass
" 2>/dev/null)

if [ -z "$free_block" ]; then
  exit 0
fi

# ── Gather context for suggestion ──
echo "Free time detected: $free_block"
echo "Current time: $(date '+%H:%M')"
echo ""

# Watchlater items (include URL so Claude can recommend with link)
watchlater="$MEMORY_DIR/system/watchlater.jsonl"
if [ -f "$watchlater" ]; then
  # Show pending (not-yet-watched) items with URLs
  pending=$(python3 -c "
import json, sys
items = []
for line in open('$watchlater'):
    try:
        e = json.loads(line.strip())
        if e.get('status', 'pending') == 'pending':
            items.append(e)
    except:
        pass
for e in items[-5:]:
    title = e.get('title', '')
    url = e.get('url', '')
    print(f'  - {title} | {url}')
" 2>/dev/null)
  if [ -n "$pending" ]; then
    echo "=== SAVED FOR LATER ==="
    echo "$pending"
    echo ""
  fi
fi

# Pending todos (brief)
todos_file="$MEMORY_DIR/system/todos.md"
if [ -f "$todos_file" ]; then
  in_progress=$(grep -i "in.progress" "$todos_file" 2>/dev/null | head -3)
  if [ -n "$in_progress" ]; then
    echo "=== IN PROGRESS ==="
    echo "$in_progress"
    echo ""
  fi
fi

# Note: nudge count is updated by the post-script only if a message is actually sent
