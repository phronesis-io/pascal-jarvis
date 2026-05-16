#!/usr/bin/env bash
# Pre-hook: gather data for evening daily reflection.
# Only runs 21:00-22:30. Provides today's activity log + morning plan.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

# Time gate: only 21:00-22:30
hour=$(date +%H)
min=$(date +%M)
if [ "$hour" -lt 21 ] || { [ "$hour" -eq 22 ] && [ "$min" -ge 30 ]; } || [ "$hour" -ge 23 ]; then
  exit 0
fi

today=$(date '+%Y-%m-%d')
echo "Daily reflection for $today ($(date '+%A'))"
echo ""

# ── 1. Today's activity log ──
activity_log="$MEMORY_DIR/system/activity_log.jsonl"
if [ -f "$activity_log" ]; then
  today_activities=$(grep "\"$today\"" "$activity_log" 2>/dev/null)
  if [ -n "$today_activities" ]; then
    echo "=== WHAT ACTUALLY HAPPENED TODAY ==="
    echo "$today_activities" | python3 -c "
import json, sys
entries = []
for line in sys.stdin:
    try:
        e = json.loads(line.strip())
        entries.append(e)
    except:
        pass
entries.sort(key=lambda x: x.get('time', ''))
for e in entries:
    energy = f' [{e[\"energy\"]}]' if e.get('energy', 'unknown') != 'unknown' else ''
    print(f'  {e.get(\"time\",\"?\")} — {e.get(\"activity\",\"?\")} ({e.get(\"source\",\"?\")}){energy}')
" 2>/dev/null
    echo ""
  else
    echo "=== No activity recorded today ==="
    echo ""
  fi
else
  echo "=== No activity log exists yet ==="
  echo ""
fi

# ── 2. Morning plan (if one was sent today) ──
plan_log="$MEMORY_DIR/system/daily_plan_log.jsonl"
if [ -f "$plan_log" ]; then
  today_plan=$(grep "\"$today\"" "$plan_log" 2>/dev/null | tail -1)
  if [ -n "$today_plan" ]; then
    echo "=== MORNING PLAN ==="
    echo "$today_plan" | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        e = json.loads(line.strip())
        print(f'  {e.get(\"plan\", \"(no plan)\")}')
    except:
        pass
" 2>/dev/null
    echo ""
  fi
fi

# ── 3. Calendar events that were scheduled today ──
calendar_file="$MEMORY_DIR/hot/calendar_today.md"
if [ -f "$calendar_file" ]; then
  echo "=== CALENDAR (what was scheduled) ==="
  python3 -c "
import re
content = open('$calendar_file').read()
lines = content.split('\n')
in_today = False
for line in lines:
    if re.match(r'.*([Tt]oday|今天)', line):
        in_today = True
        print(line)
        continue
    if in_today:
        if re.match(r'.*(Tomorrow|明天|Day \d|后天|周)', line):
            break
        if line.strip():
            print(line)
" 2>/dev/null
  echo ""
fi

# ── 4. Recent patterns (for context) ──
patterns_file="$MEMORY_DIR/system/patterns.jsonl"
if [ -f "$patterns_file" ]; then
  recent_patterns=$(tail -3 "$patterns_file" 2>/dev/null)
  if [ -n "$recent_patterns" ]; then
    echo "=== RECENT PATTERNS OBSERVED ==="
    echo "$recent_patterns" | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        e = json.loads(line.strip())
        print(f'  - {e.get(\"pattern\", \"\")}')
    except:
        pass
" 2>/dev/null
  fi
fi
