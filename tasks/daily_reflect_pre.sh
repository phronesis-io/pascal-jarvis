#!/usr/bin/env bash
# Pre-hook: gather data for evening daily reflection.
# Only runs 21:00-22:30. Provides today's activity log + morning plan.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
if ! PYTHONPATH="$JARVIS_DIR" JARVIS_DIR="$JARVIS_DIR" \
    python3 -m core.retained_rhythms enabled daily_reflect >/dev/null 2>&1; then
  exit 0
fi

# Load configurable time windows from jarvis.yaml
eval $(bash "$JARVIS_DIR/scripts/config_env.sh" 2>/dev/null) || true

# Time gate (configurable via schedule.daily_reflect_window in jarvis.yaml)
hour=$(date +%H)
min=$(date +%M)
_rs_h="${REFLECT_START_HOUR:-20}"; _rs_m="${REFLECT_START_MIN:-30}"
_re_h="${REFLECT_END_HOUR:-23}"; _re_m="${REFLECT_END_MIN:-30}"
if [ "$hour" -lt "$_rs_h" ] || { [ "$hour" -eq "$_rs_h" ] && [ "$min" -lt "$_rs_m" ]; } || { [ "$hour" -eq "$_re_h" ] && [ "$min" -ge "$_re_m" ]; }; then
  exit 0
fi

# Dedup: skip if already succeeded today (prevents double-fire on restart)
_stamp="$JARVIS_DIR/data/.daily_reflect_stamp"
if [ -f "$_stamp" ] && [ "$(cat "$_stamp" 2>/dev/null)" = "$(date +%Y-%m-%d)" ]; then
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
