#!/usr/bin/env bash
# Pre-hook: pull today's + tomorrow's calendar from Lark and output as context.
# Runs every 30m to keep hot/calendar_today.md fresh.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

# Require lark-cli
command -v lark-cli &>/dev/null || exit 0

# Today's agenda
today_iso=$(date -u +%Y-%m-%dT00:00:00Z)
today_end=$(date -v+1d -u +%Y-%m-%dT00:00:00Z 2>/dev/null || date -d '+1 day' -u +%Y-%m-%dT00:00:00Z)
tomorrow_end=$(date -v+2d -u +%Y-%m-%dT00:00:00Z 2>/dev/null || date -d '+2 days' -u +%Y-%m-%dT00:00:00Z)

today_data=$(lark-cli calendar +agenda --as user --format json --start "$today_iso" --end "$today_end" 2>/dev/null)
tomorrow_data=$(lark-cli calendar +agenda --as user --format json --start "$today_end" --end "$tomorrow_end" 2>/dev/null)

# Format via Python
export TODAY_DATA="$today_data" TOMORROW_DATA="$tomorrow_data"
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
        # Format times
        try:
            s = datetime.fromisoformat(start_dt).astimezone(tz).strftime('%H:%M')
            e = datetime.fromisoformat(end_dt).astimezone(tz).strftime('%H:%M')
            time_str = f'{s}-{e}'
        except Exception:
            time_str = '??:??'
        location = item.get('location', '')
        desc = item.get('description', '')
        events.append({'time': time_str, 'summary': summary,
                       'location': location, 'description': desc[:100]})
    return events

today = parse_events(os.environ.get('TODAY_DATA', ''))
tomorrow = parse_events(os.environ.get('TOMORROW_DATA', ''))

if not today and not tomorrow:
    sys.exit(0)

now = datetime.now(tz)
print(f'Calendar sync at {now.strftime(\"%H:%M\")}')
print(f'Today ({now.strftime(\"%Y-%m-%d %A\")}):')
if today:
    for e in today:
        line = f'  {e[\"time\"]}  {e[\"summary\"]}'
        if e['location']:
            line += f'  @ {e[\"location\"]}'
        print(line)
else:
    print('  (no events)')

tomorrow_date = now + timedelta(days=1)
print(f'Tomorrow ({tomorrow_date.strftime(\"%Y-%m-%d %A\")}):')
if tomorrow:
    for e in tomorrow:
        line = f'  {e[\"time\"]}  {e[\"summary\"]}'
        if e['location']:
            line += f'  @ {e[\"location\"]}'
        print(line)
else:
    print('  (no events)')
" 2>/dev/null
