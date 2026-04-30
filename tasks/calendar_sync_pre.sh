#!/usr/bin/env bash
# Pre-hook: pull today's + tomorrow's calendar from Lark, inject user interests.
# Runs every 30m to keep hot/calendar_today.md fresh.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

# Require lark-cli
command -v lark-cli &>/dev/null || exit 0

# Today + tomorrow + day-after (3-day window)
today_iso=$(date -u +%Y-%m-%dT00:00:00Z)
today_end=$(date -v+1d -u +%Y-%m-%dT00:00:00Z 2>/dev/null || date -d '+1 day' -u +%Y-%m-%dT00:00:00Z)
tomorrow_end=$(date -v+2d -u +%Y-%m-%dT00:00:00Z 2>/dev/null || date -d '+2 days' -u +%Y-%m-%dT00:00:00Z)
day3_end=$(date -v+3d -u +%Y-%m-%dT00:00:00Z 2>/dev/null || date -d '+3 days' -u +%Y-%m-%dT00:00:00Z)

today_data=$(lark-cli calendar +agenda --as user --format json --start "$today_iso" --end "$today_end" 2>/dev/null)
tomorrow_data=$(lark-cli calendar +agenda --as user --format json --start "$today_end" --end "$tomorrow_end" 2>/dev/null)
day3_data=$(lark-cli calendar +agenda --as user --format json --start "$tomorrow_end" --end "$day3_end" 2>/dev/null)

# Load user interests file (if exists)
interests_file="$MEMORY_DIR/warm/interests.md"
interests=""
if [ -f "$interests_file" ]; then
  interests=$(cat "$interests_file")
fi

# Format via Python
export TODAY_DATA="$today_data" TOMORROW_DATA="$tomorrow_data" DAY3_DATA="$day3_data" INTERESTS="$interests"
python3 -c "
import json, os, sys
from datetime import datetime, timezone, timedelta

tz = timezone(timedelta(hours=8))

def parse_events(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else data.get('data', data.get('items', []))
    if not isinstance(items, list):
        return []
    events = []
    for item in items:
        summary = item.get('summary', item.get('title', '(untitled)'))
        start = item.get('start_time', {})
        end = item.get('end_time', {})
        start_dt = start.get('datetime', start.get('date', ''))
        end_dt = end.get('datetime', end.get('date', ''))
        try:
            s = datetime.fromisoformat(start_dt).astimezone(tz).strftime('%H:%M')
            e = datetime.fromisoformat(end_dt).astimezone(tz).strftime('%H:%M')
            time_str = f'{s}-{e}'
        except Exception:
            time_str = '??:??'
        location = item.get('location', '')
        desc = item.get('description', '')
        status = item.get('status', '')
        events.append({'time': time_str, 'summary': summary,
                       'location': location, 'description': desc[:200],
                       'status': status})
    return events

def print_day(label, events):
    print(f'{label}:')
    if events:
        for e in events:
            line = f'  {e[\"time\"]}  {e[\"summary\"]}'
            if e['location']:
                line += f'  @ {e[\"location\"]}'
            if e['description']:
                line += f'  ({e[\"description\"]})'
            print(line)
    else:
        print('  (no events)')

today = parse_events(os.environ.get('TODAY_DATA', ''))
tomorrow = parse_events(os.environ.get('TOMORROW_DATA', ''))
day3 = parse_events(os.environ.get('DAY3_DATA', ''))
interests = os.environ.get('INTERESTS', '').strip()

# Always output — even empty days are useful context for proactive suggestions
now = datetime.now(tz)
print(f'Calendar sync at {now.strftime(\"%H:%M\")} (current time: {now.strftime(\"%Y-%m-%d %A %H:%M\")})')
print()

print_day(f'Today ({now.strftime(\"%Y-%m-%d %A\")})', today)
print()
d1 = now + timedelta(days=1)
print_day(f'Tomorrow ({d1.strftime(\"%Y-%m-%d %A\")})', tomorrow)
print()
d2 = now + timedelta(days=2)
print_day(f'Day after ({d2.strftime(\"%Y-%m-%d %A\")})', day3)

if interests:
    print()
    print('=== USER INTERESTS (check for upcoming events) ===')
    print(interests)
" 2>/dev/null
