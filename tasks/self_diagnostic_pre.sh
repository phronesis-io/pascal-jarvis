#!/usr/bin/env bash
# Pre-hook: collect system health data for self-diagnostic
WORK_DIR="${WORK_DIR:-/Users/pascal/Desktop/jarvis}"
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.claude/projects/-Users-pascal-Desktop-jarvis/memory}"

echo "=== SYSTEM HEALTH CHECK ==="
echo "Time: $(date '+%Y-%m-%d %H:%M %A')"
echo ""

# 1. EigenFlux profile staleness
echo "--- EigenFlux Profile ---"
profile_ts=$(eigenflux profile show 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
ts = d.get('profile', {}).get('updated_at', 0) / 1000
from datetime import datetime
print(datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M'))
" 2>/dev/null || echo "unknown")
echo "Last updated: $profile_ts"

# 2. Calendar freshness
echo ""
echo "--- Calendar ---"
cal_file="$MEMORY_DIR/calendar_today.md"
[ ! -f "$cal_file" ] && cal_file="$MEMORY_DIR/hot/calendar_today.md"
if [ -f "$cal_file" ]; then
  cal_sync=$(grep -o 'synced [0-9-]* [0-9:]*' "$cal_file" | head -1)
  echo "Last sync: $cal_sync"
else
  echo "⚠️ No calendar_today.md found"
fi

# 3. Repos — last pull times
echo ""
echo "--- Repos ---"
for repo in "$WORK_DIR/repos"/*/; do
  [ -d "$repo/.git" ] || continue
  name=$(basename "$repo")
  last_fetch=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$repo/.git/FETCH_HEAD" 2>/dev/null || echo "never")
  echo "  $name: last fetch $last_fetch"
done

# 4. Personal site
echo ""
echo "--- Personal Site ---"
if [ -d "$WORK_DIR/repos/huyongyi-cpu.github.io" ]; then
  last_commit=$(git -C "$WORK_DIR/repos/huyongyi-cpu.github.io" log -1 --format="%ci %s" 2>/dev/null || echo "unknown")
  echo "Last commit: $last_commit"
else
  echo "⚠️ Repo not found"
fi

# 5. Memory health
echo ""
echo "--- Memory ---"
hot_count=$(ls "$MEMORY_DIR/hot/"*.md 2>/dev/null | wc -l | tr -d ' ')
warm_count=$(ls "$MEMORY_DIR/warm/"*.md 2>/dev/null | wc -l | tr -d ' ')
echo "Hot files: $hot_count | Warm files: $warm_count"
echo "Behavioral rules: $([ -f "$MEMORY_DIR/hot/behavioral_rules.md" ] && echo "✓" || echo "✗")"
