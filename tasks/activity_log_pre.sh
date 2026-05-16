#!/usr/bin/env bash
# Pre-hook: gather signals from last 45 minutes for activity logging.
# Sources: conversation topics + calendar events that occurred in the window.
# This is SILENT recording — never messages the user.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
TRACKER="$JARVIS_DIR/active_sessions.json"
SESSION_DIR="${CLAUDE_PROJECT_DIR:-${SESSION_DIR:-}}"

# Only run during waking hours
hour=$(date +%H)
if [ "$hour" -lt 8 ] || [ "$hour" -ge 23 ]; then
  exit 0
fi

now_ts=$(date '+%Y-%m-%d %H:%M')
now_hm=$(date '+%H:%M')

# ── 1. Calendar events that occurred in the last 45 minutes ──
calendar_context=""
calendar_file="$MEMORY_DIR/hot/calendar_today.md"
if [ -f "$calendar_file" ]; then
  calendar_context=$(python3 -c "
import re, sys
from datetime import datetime, timedelta

now = datetime.now()
window_start = now - timedelta(minutes=45)

cal = open('$calendar_file').read()
events_in_window = []
for line in cal.split('\n'):
    m = re.match(r'\s*-?\s*(\d{2}:\d{2})-(\d{2}:\d{2})\s+(.*)', line)
    if not m:
        continue
    start_str, end_str, title = m.groups()
    try:
        start_t = datetime.strptime(start_str, '%H:%M').replace(
            year=now.year, month=now.month, day=now.day)
        end_t = datetime.strptime(end_str, '%H:%M').replace(
            year=now.year, month=now.month, day=now.day)
    except:
        continue
    # Event overlaps with window if it started or ended within last 45min
    if start_t >= window_start and start_t <= now:
        events_in_window.append(f'{start_str}-{end_str} {title.strip()}')
    elif end_t >= window_start and end_t <= now:
        events_in_window.append(f'{start_str}-{end_str} {title.strip()}')

if events_in_window:
    print('Calendar events in window:')
    for e in events_in_window:
        print(f'  {e}')
" 2>/dev/null)
fi

# ── 2. Recent conversation snippets (last 45 min) ──
conversation_context=""
if [ -n "$SESSION_DIR" ] && [ -d "$SESSION_DIR" ] && [ -f "$TRACKER" ]; then
  conversation_context=$(python3 -c "
import json, os, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

tracker_path = '$TRACKER'
sdir = Path('$SESSION_DIR')
NAMESPACE = uuid.UUID('a1b2c3d4-e5f6-7890-abcd-ef1234567890')

try:
    tracker = json.load(open(tracker_path))
except:
    raise SystemExit(0)

cutoff = (datetime.now(timezone.utc) - timedelta(minutes=45)).strftime('%Y-%m-%dT%H:%M')
msgs = []

for conv_key, entry in tracker.items():
    sid = entry.get('session_id', '')
    if not sid:
        continue
    path = sdir / f'{sid}.jsonl'
    if not path.exists():
        continue
    for line in path.open(encoding='utf-8', errors='ignore'):
        try:
            obj = json.loads(line)
        except:
            continue
        if obj.get('type') != 'user':
            continue
        ts = obj.get('timestamp', '')
        if ts < cutoff:
            continue
        msg = obj.get('message', {})
        content = msg.get('content', '')
        text = ''
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = ' '.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
        text = text.strip()
        if text:
            msgs.append(f'[{ts[:16]}] user: {text[:150]}')

if msgs:
    msgs.sort()
    print('Recent user messages (last 45min):')
    for m in msgs[-8:]:  # Last 8 messages max
        print(f'  {m}')
" 2>/dev/null)
fi

# ── 3. Check if user explicitly mentioned activities ──
# (handled by Claude from the conversation context above)

# ── Output ──
if [ -z "$calendar_context" ] && [ -z "$conversation_context" ]; then
  exit 0  # No signals at all
fi

echo "Activity log window: $now_ts (last 45 minutes)"
echo ""
[ -n "$calendar_context" ] && echo "$calendar_context" && echo ""
[ -n "$conversation_context" ] && echo "$conversation_context"
