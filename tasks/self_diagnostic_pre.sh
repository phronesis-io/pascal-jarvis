#!/usr/bin/env bash
# Pre-hook: collect system health data for self-diagnostic
JARVIS_DIR="/Users/pascal/Desktop/jarvis"
MEMORY_DIR="$HOME/.claude/projects/-Users-pascal-Desktop-jarvis/memory"

echo "=== SYSTEM HEALTH CHECK ==="
echo "Time: $(date '+%Y-%m-%d %H:%M %A')"
echo ""

# 1. EigenFlux profile staleness
echo "--- EigenFlux Profile ---"
profile_ts=$(python3 -c "
import json
p = json.load(open('$HOME/.eigenflux/servers/eigenflux/profile.json'))
ts = p.get('updated_at', 0) / 1000
from datetime import datetime
print(datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M'))
" 2>/dev/null || echo "unknown")
echo "Last updated: $profile_ts"

# 2. Calendar freshness
echo ""
echo "--- Calendar ---"
if [ -f "$MEMORY_DIR/calendar_today.md" ]; then
  cal_sync=$(grep -o 'synced [0-9-]* [0-9:]*' "$MEMORY_DIR/calendar_today.md" | head -1)
  echo "Last sync: $cal_sync"
else
  echo "⚠️ No calendar_today.md found"
fi

# 3. Repos — last pull times
echo ""
echo "--- Repos ---"
for repo in "$JARVIS_DIR/repos"/*/; do
  [ -d "$repo/.git" ] || continue
  name=$(basename "$repo")
  last_fetch=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$repo/.git/FETCH_HEAD" 2>/dev/null || echo "never")
  echo "  $name: last fetch $last_fetch"
done

# 4. Personal site
echo ""
echo "--- Personal Site ---"
if [ -d "$JARVIS_DIR/repos/huyongyi-cpu.github.io" ]; then
  last_commit=$(git -C "$JARVIS_DIR/repos/huyongyi-cpu.github.io" log -1 --format="%ci %s" 2>/dev/null || echo "unknown")
  echo "Last commit: $last_commit"
else
  echo "⚠️ Repo not found"
fi

# 5. Memory health
echo ""
echo "--- Memory ---"
echo "Files: $(ls "$MEMORY_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')"
pending=$(grep -c "^→ " "$MEMORY_DIR/pending_updates.md" 2>/dev/null || echo "0")
echo "Pending updates: $pending"

# 6. Recent feedback memories (things Pascal corrected)
echo ""
echo "--- Recent Feedback ---"
for f in "$MEMORY_DIR"/feedback_*.md; do
  [ -f "$f" ] || continue
  echo "  $(basename "$f")"
done
