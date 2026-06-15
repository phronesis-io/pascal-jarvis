#!/usr/bin/env bash
# Pre-hook: detect approaching or current free blocks and surface them.
#
# REQ-75 (event-gate low-value proactive sources): the bare "你有空" nudge
# earned only ~7% engagement and double-fired (a pre-block fire + an in-progress
# fire ~60min apart in the same block). Per Pascal's principle — every proactive
# message must earn its interruption — this pre-script now:
#   1. emits NOTHING unless there is genuine content to offer in this free
#      block: an unconsumed watch-later item OR a fitting todo/deadline.
#      (The bare time-only nudge is dropped entirely → heartbeat skips.)
#   2. collapses the double-fire to at most ONE message per free block, via a
#      per-block fire stamp in .free_time_nudge_state (in addition to the
#      existing daily rate limit).
#
# Rate-limited to max 2 nudges per day (line 2 of the state file), AND at most
# one fire per distinct free block (line 3 of the state file).

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
# State file format (up to 3 lines):
#   line 1: YYYY-MM-DD   (today, for daily reset)
#   line 2: <int>        (today's nudge count, incremented by the post-script)
#   line 3: <block_id>   (id of the last free block we fired for — per-block dedup)
STATE_FILE="$JARVIS_DIR/.free_time_nudge_state"
today=$(date '+%Y-%m-%d')
nudge_count=0
last_block_id=""
if [ -f "$STATE_FILE" ]; then
  state_date=$(sed -n '1p' "$STATE_FILE" 2>/dev/null)
  if [ "$state_date" = "$today" ]; then
    nudge_count=$(sed -n '2p' "$STATE_FILE" 2>/dev/null || echo 0)
    last_block_id=$(sed -n '3p' "$STATE_FILE" 2>/dev/null || echo "")
  fi
fi
# Guard against a malformed count line
case "$nudge_count" in
  ''|*[!0-9]*) nudge_count=0 ;;
esac

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
            # BLOCK_ID keyed on the block's END boundary (the next event), NOT on
            # 'now' — so a pre-block fire and an in-progress fire inside the SAME
            # gap collapse to one id and the second is suppressed downstream.
            print(f'BLOCK_ID: {now.strftime(\"%Y-%m-%d\")}|until-{next_event[0]}|{next_event[1]}')
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
            print(f'BLOCK_ID: {now.strftime(\"%Y-%m-%d\")}|until-eod|no-more-events')
            print(f'FREE_BLOCK: now until end of day ({gap_min}min), no more events')
    except:
        pass
" 2>/dev/null)

if [ -z "$free_block" ]; then
  exit 0
fi

# Split out the block id (line 1) from the human-readable block line (line 2).
block_id=$(printf '%s\n' "$free_block" | sed -n 's/^BLOCK_ID: //p' | head -1)
free_block=$(printf '%s\n' "$free_block" | grep '^FREE_BLOCK:' | head -1)

if [ -z "$free_block" ]; then
  exit 0
fi

# Per-block dedup: if we already fired for this exact free block today, skip.
# This collapses the historical double-fire (pre-block + in-progress) into one.
if [ -n "$block_id" ] && [ "$block_id" = "$last_block_id" ]; then
  exit 0
fi

# ── Gather genuine content for this free block (REQ-75 event gate) ──
# We only interrupt if there's something concrete to offer: an unconsumed
# watch-later item OR a fitting todo/deadline. The bare "你有空" nudge is gone.

# Watchlater items (include URL so Claude can recommend with link)
watchlater="$MEMORY_DIR/system/watchlater.jsonl"
pending=""
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
fi

# Pending todos / deadlines that fit the block (brief)
todos_file="$MEMORY_DIR/system/todos.md"
todos=""
if [ -f "$todos_file" ]; then
  todos=$(grep -iE "in.progress|due|deadline|- \[ \]" "$todos_file" 2>/dev/null | head -3)
fi

# Event gate: if there's NO watch-later item and NO fitting todo/deadline,
# stay silent. Empty pre output → heartbeat skips the task (established
# contract). This is the whole point of REQ-75.
if [ -z "$pending" ] && [ -z "$todos" ]; then
  exit 0
fi

# ── There IS something worth offering — surface it ──
echo "Free time detected: $free_block"
echo "Current time: $(date '+%H:%M')"
echo ""

if [ -n "$pending" ]; then
  echo "=== SAVED FOR LATER ==="
  echo "$pending"
  echo ""
fi

if [ -n "$todos" ]; then
  echo "=== TODOS / DEADLINES THAT FIT ==="
  echo "$todos"
  echo ""
fi

# Stamp this free block so a second fire in the SAME block is suppressed
# (collapses the historical pre-block + in-progress double-fire). We persist
# the block id on line 3; the daily date/count on lines 1-2 are owned by the
# post-script (which only counts when a message is actually sent).
if [ -n "$block_id" ]; then
  printf '%s\n%s\n%s\n' "$today" "$nudge_count" "$block_id" > "$STATE_FILE"
fi

# Note: nudge count is updated by the post-script only if a message is actually sent
