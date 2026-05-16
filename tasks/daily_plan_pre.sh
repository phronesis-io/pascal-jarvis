#!/usr/bin/env bash
# Pre-hook: gather data for morning daily plan.
# Only runs 8:00-9:30. Provides today's calendar, free blocks, pending todos.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

# Time gate: only 8:00-9:30
hour=$(date +%H)
min=$(date +%M)
if [ "$hour" -lt 8 ] || { [ "$hour" -eq 9 ] && [ "$min" -ge 30 ]; } || [ "$hour" -ge 10 ]; then
  exit 0
fi

echo "Daily plan for $(date '+%Y-%m-%d %A')"
echo ""

# ── 1. Today's calendar ──
calendar_file="$MEMORY_DIR/hot/calendar_today.md"
if [ -f "$calendar_file" ]; then
  # Extract just today's section
  echo "=== TODAY'S CALENDAR ==="
  python3 -c "
import re
content = open('$calendar_file').read()
# Find 'Today' or '今天' section, print until next day header
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
        print(line)
" 2>/dev/null
  echo ""
fi

# ── 2. Free blocks today (computed from calendar) ──
echo "=== FREE BLOCKS (>30min) ==="
python3 -c "
import re
from datetime import datetime

content = open('$calendar_file').read() if __import__('os').path.exists('$calendar_file') else ''
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
        m = re.match(r'\s*-?\s*(\d{2}:\d{2})-(\d{2}:\d{2})', line)
        if m:
            events.append((m.group(1), m.group(2)))

# Compute free blocks between 8:00 and 22:00
day_start = '08:00'
day_end = '22:00'
events.sort()

boundaries = [(day_start, events[0][0])] if events else [(day_start, day_end)]
for i in range(len(events) - 1):
    boundaries.append((events[i][1], events[i+1][0]))
if events:
    boundaries.append((events[-1][1], day_end))

for start, end in boundaries:
    try:
        s = datetime.strptime(start, '%H:%M')
        e = datetime.strptime(end, '%H:%M')
        dur = (e - s).seconds // 60
        if dur >= 30:
            print(f'  {start}-{end} ({dur}min)')
    except:
        pass
" 2>/dev/null
echo ""

# ── 3. Pending todos ──
todos_file="$MEMORY_DIR/system/todos.md"
if [ -f "$todos_file" ]; then
  echo "=== PENDING TODOS ==="
  # Show items marked in_progress
  grep -i "in.progress\|pending\|- \[ \]" "$todos_file" 2>/dev/null | head -5
  echo ""
fi

# ── 4. Lark Tasks (if available) ──
if command -v lark-cli &>/dev/null; then
  lark_tasks=$(lark-cli task +get-my-tasks --as user --format json 2>/dev/null)
  if [ -n "$lark_tasks" ] && [ "$lark_tasks" != "null" ] && [ "$lark_tasks" != "[]" ]; then
    echo "=== LARK TASKS ==="
    echo "$lark_tasks" | python3 -c "
import json, sys
try:
    tasks = json.load(sys.stdin)
    if isinstance(tasks, list):
        for t in tasks[:5]:
            title = t.get('summary', t.get('title', ''))
            due = t.get('due', '')
            if title:
                print(f'  - {title}' + (f' (due: {due})' if due else ''))
except:
    pass
" 2>/dev/null
    echo ""
  fi
fi

# ── 5. Yesterday's activity (for continuity) ──
activity_log="$MEMORY_DIR/system/activity_log.jsonl"
if [ -f "$activity_log" ]; then
  yesterday=$(date -v-1d '+%Y-%m-%d' 2>/dev/null || date -d "yesterday" '+%Y-%m-%d')
  yesterday_activities=$(grep "\"$yesterday\"" "$activity_log" 2>/dev/null | tail -5)
  if [ -n "$yesterday_activities" ]; then
    echo "=== YESTERDAY'S ACTIVITIES (for continuity) ==="
    echo "$yesterday_activities" | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        e = json.loads(line.strip())
        print(f'  {e.get(\"time\",\"\")} {e.get(\"activity\",\"\")}')
    except:
        pass
" 2>/dev/null
  fi
fi
