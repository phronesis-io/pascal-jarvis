#!/usr/bin/env bash
# Pre-hook: pull 30-day rolling calendar from Lark, inject user interests.
# Days 1-7: detailed view (time, location, description)
# Days 8-30: compact view (only dates with events, title only)
# Runs every 30m to keep hot/calendar_today.md fresh.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
RAW_CACHE="$MEMORY_DIR/system/.calendar_raw_output.txt"

# Require lark-cli
command -v lark-cli &>/dev/null || exit 0

# ── Stale date detection: if calendar_today.md is from a different day, force refresh ──
calendar_file="$MEMORY_DIR/hot/calendar_today.md"
if [ -f "$calendar_file" ]; then
  synced_date=$(grep -o 'synced [0-9-]*' "$calendar_file" 2>/dev/null | head -1 | cut -d' ' -f2)
  today_date=$(date '+%Y-%m-%d')
  if [ -n "$synced_date" ] && [ "$synced_date" != "$today_date" ]; then
    echo "[calendar-sync] Stale date detected ($synced_date vs $today_date), forcing refresh" >&2
  fi
fi

# 30-day rolling window — use Beijing time (UTC+8) for day boundaries
# so DAY0 matches "Today" in the Python display formatter below.
# Previously used UTC, causing 8-hour misalignment at midnight Beijing time
# (e.g., yesterday's 19:30 climbing event labeled as today's).
day_boundaries=$(python3 -c "
from datetime import datetime, timedelta, timezone
tz = timezone(timedelta(hours=8))
today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
utc = timezone.utc
for i in range(31):
    dt = (today + timedelta(days=i)).astimezone(utc)
    print(dt.strftime('%Y-%m-%dT%H:%M:%SZ'))
")
IFS=$'\n' read -r -d '' -a BOUNDS <<< "$day_boundaries" || true

# Build day boundaries and fetch events for each day
for i in $(seq 0 29); do
  day_start="${BOUNDS[$i]}"
  day_end="${BOUNDS[$((i+1))]}"
  day_data=$(lark-cli calendar +agenda --as user --format json --start "$day_start" --end "$day_end" 2>/dev/null)
  export "DAY${i}_DATA=$day_data"
done

# Load user interests file (if exists)
interests_file="$MEMORY_DIR/warm/interests.md"
interests=""
if [ -f "$interests_file" ]; then
  interests=$(cat "$interests_file")
fi

# Format via Python (all DAY0_DATA..DAY6_DATA + INTERESTS are already exported)
export INTERESTS="$interests"
mkdir -p "$(dirname "$RAW_CACHE")"

# Use a subshell + tee to save raw output to cache while still printing to stdout
(
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
        event_id = item.get('event_id', '')
        calendar_id = item.get('organizer_calendar_id', '')
        events.append({'time': time_str, 'summary': summary,
                       'location': location, 'description': desc[:200],
                       'status': status, 'event_id': event_id,
                       'calendar_id': calendar_id,
                       'start_iso': start_dt, 'end_iso': end_dt})
    return events

now = datetime.now(tz)
print(f'Calendar sync at {now.strftime(\"%H:%M\")} (current time: {now.strftime(\"%Y-%m-%d %A %H:%M\")})')
print()

day_labels = ['Today', 'Tomorrow'] + [f'Day {i+1}' for i in range(2, 30)]
all_events_map = []  # collect event_id mapping for write-back

# Days 0-6: detailed view
for i in range(7):
    day_dt = now + timedelta(days=i)
    events = parse_events(os.environ.get(f'DAY{i}_DATA', ''))
    label = f'{day_labels[i]} ({day_dt.strftime(\"%Y-%m-%d %A\")})'
    print(f'{label}:')
    if events:
        for e in events:
            line = f'  {e[\"time\"]}  {e[\"summary\"]}'
            if e['location']:
                line += f'  @ {e[\"location\"]}'
            if e['description']:
                line += f'  ({e[\"description\"]})'
            print(line)
            if e.get('event_id'):
                all_events_map.append({
                    'event_id': e['event_id'],
                    'calendar_id': e.get('calendar_id', ''),
                    'summary': e['summary'],
                    'time': e['time'],
                    'date': day_dt.strftime('%Y-%m-%d'),
                    'start_iso': e.get('start_iso', ''),
                    'end_iso': e.get('end_iso', ''),
                })
    else:
        print('  (no events)')
    print()

# Days 7-29: compact view (only show days with events)
compact_lines = []
for i in range(7, 30):
    day_dt = now + timedelta(days=i)
    events = parse_events(os.environ.get(f'DAY{i}_DATA', ''))
    if events:
        date_str = day_dt.strftime('%m/%d %a')
        for e in events:
            compact_lines.append(f'  {date_str}  {e[\"time\"]}  {e[\"summary\"]}')
            if e.get('event_id'):
                all_events_map.append({
                    'event_id': e['event_id'],
                    'calendar_id': e.get('calendar_id', ''),
                    'summary': e['summary'],
                    'time': e['time'],
                    'date': day_dt.strftime('%Y-%m-%d'),
                    'start_iso': e.get('start_iso', ''),
                    'end_iso': e.get('end_iso', ''),
                })

if compact_lines:
    print('Upcoming (Day 8-30, events only):')
    for line in compact_lines:
        print(line)
    print()

interests = os.environ.get('INTERESTS', '').strip()
if interests:
    print('=== USER INTERESTS (for context only — do NOT fabricate events) ===')
    print(interests)

# Save event mapping file for calendar write-back
if all_events_map:
    import pathlib
    jarvis_dir = os.environ.get('JARVIS_DIR', '')
    if jarvis_dir:
        mf = pathlib.Path(jarvis_dir) / 'calendar_event_mapping.json'
        try:
            mf.write_text(json.dumps(all_events_map, ensure_ascii=False, indent=2))
            print(f'[calendar] {len(all_events_map)} events mapped', file=sys.stderr)
        except Exception:
            pass

" 2>/dev/null

# ── Fetch real NBA schedule for teams in interests ──
# Only fetches if interests mention NBA/骑士/Cavaliers
if echo "$interests" | grep -qi 'cavaliers\|骑士\|NBA'; then
  nba_schedule=$(curl -s --max-time 10 'https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json' 2>/dev/null | python3 -c "
import json, sys
from datetime import datetime, timezone, timedelta

try:
    data = json.load(sys.stdin)
except:
    sys.exit(0)

dates = data.get('leagueSchedule', {}).get('gameDates', [])
tz_cn = timezone(timedelta(hours=8))
now = datetime.now(timezone.utc)

# Team codes to track (extend as needed)
teams = {'CLE'}
games = []
for gd in dates:
    for game in gd.get('games', []):
        home = game.get('homeTeam', {}).get('teamTricode', '')
        away = game.get('awayTeam', {}).get('teamTricode', '')
        if not teams & {home, away}:
            continue
        dt_str = game.get('gameDateTimeUTC', '')
        status = game.get('gameStatusText', '')
        series = game.get('seriesText', '')
        try:
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            if dt < now - timedelta(hours=6):
                continue
            cn = dt.astimezone(tz_cn)
            opponent = away if home == 'CLE' else home
            ha = 'Home' if home == 'CLE' else 'Away'
            games.append(f'{cn.strftime(\"%m/%d %H:%M\")} CLE vs {opponent} ({ha}) {series} [{status}]')
        except:
            pass
        if len(games) >= 5:
            break
    if len(games) >= 5:
        break

if games:
    print()
    print('=== REAL NBA SCHEDULE (verified from nba.com API) ===')
    for g in games:
        print(f'  {g}')
" 2>/dev/null || true)
  if [ -n "$nba_schedule" ]; then
    echo ""
    echo "$nba_schedule"
  fi
fi
) | tee "$RAW_CACHE"
