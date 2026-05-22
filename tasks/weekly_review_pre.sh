#!/usr/bin/env bash
# Pre-hook: gather full landscape for weekly review
# Only runs Sunday 10:00-12:00
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

dow=$(date +%u)  # 7 = Sunday
hour=$(date +%H)
if [ "$dow" -ne 7 ] || [ "$hour" -lt 10 ] || [ "$hour" -ge 12 ]; then
  exit 0
fi

echo "=== WEEKLY REVIEW: $(date '+%Y-%m-%d') ==="
echo ""

python3 -c "
import sys, json
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager

tm = TaskManager(Path('$MEMORY_DIR'))

# Active tasks
active = tm.active()
print('=== ACTIVE TASKS ===')
for t in active:
    print(f'  [{t[\"status\"]}] {t[\"id\"]}: {t[\"title\"]} (type={t[\"type\"]}, energy={t[\"energy\"]}, decay={t[\"decay_touches\"]})')

print()

# Praxis
print('=== PRAXIS ===')
for p in tm.praxis_list():
    print(f'  {p[\"id\"]}: {p[\"title\"]} (streak={p[\"streak_current\"]}, best={p[\"streak_best\"]}, freq={p[\"frequency\"]})')

print()

# Resolved this week
cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
tasks_file = Path('$MEMORY_DIR') / 'system' / 'tasks.jsonl'
if tasks_file.exists():
    print('=== RESOLVED THIS WEEK ===')
    for line in tasks_file.read_text().splitlines():
        if not line.strip():
            continue
        t = json.loads(line)
        if t.get('resolved_date', '') >= cutoff and t.get('resolution'):
            print(f'  {t[\"resolution\"]}: {t[\"title\"]}')
    print()
" 2>/dev/null

# Projects
echo "=== PROJECTS ==="
todos="$MEMORY_DIR/system/todos.md"
if [ -f "$todos" ]; then
  grep -E "^### |状态" "$todos" 2>/dev/null | head -20
fi
echo ""

# Engagement insights & adaptations (if available)
insights_file="$MEMORY_DIR/system/engagement_insights.md"
if [ -f "$insights_file" ]; then
  echo "=== ENGAGEMENT INSIGHTS ==="
  cat "$insights_file"
  echo ""
fi

# Next week calendar density
echo "=== NEXT WEEK DENSITY ==="
calendar_file="$MEMORY_DIR/hot/calendar_today.md"
if [ -f "$calendar_file" ]; then
  cat "$calendar_file"
fi
