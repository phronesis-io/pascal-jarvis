#!/usr/bin/env bash
# Pre-hook: pull 7-day rolling calendar from Lark, inject user interests.
# Runs every 30m to keep hot/calendar_today.md fresh.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

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

# 7-day rolling window
today_iso=$(date -u +%Y-%m-%dT00:00:00Z)

# Build day boundaries and fetch events for each day
for i in $(seq 0 6); do
  day_start=$(date -v+${i}d -u +%Y-%m-%dT00:00:00Z 2>/dev/null || date -d "+${i} days" -u +%Y-%m-%dT00:00:00Z)
  day_end=$(date -v+$((i+1))d -u +%Y-%m-%dT00:00:00Z 2>/dev/null || date -d "+$((i+1)) days" -u +%Y-%m-%dT00:00:00Z)
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

now = datetime.now(tz)
print(f'Calendar sync at {now.strftime(\"%H:%M\")} (current time: {now.strftime(\"%Y-%m-%d %A %H:%M\")})')
print()

day_labels = ['Today', 'Tomorrow'] + [f'Day {i+1}' for i in range(2, 7)]
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
    else:
        print('  (no events)')
    print()

interests = os.environ.get('INTERESTS', '').strip()
if interests:
    print('=== USER INTERESTS (for context only — do NOT fabricate events) ===')
    print(interests)
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
