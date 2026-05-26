#!/usr/bin/env bash
# Pre-hook: detect stale/overdue tasks for triage
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

python3 -c "
import sys, json
sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path

tm = TaskManager(Path('$MEMORY_DIR'))
stale = tm.stale_inbox(hours=48)
decay_ready = tm.ready_to_decay(threshold=3)
# Also get overdue committed items for today
from datetime import datetime
today = datetime.now().strftime('%Y-%m-%d')
committed = tm.today_committed(today)
overdue = [t for t in committed if t.get('when') and t['when'][:10] < today]

if not stale and not decay_ready and not overdue:
    sys.exit(0)  # empty stdout = skip task

# Touch stale items so they don't immediately re-trigger next cycle.
# After 3 touches → ready_to_decay → auto-decay (mercy, not punishment).
for t in stale:
    tm.touch(t['id'])

output = []
if stale:
    output.append('=== STALE INBOX (>48h) ===')
    for t in stale:
        output.append(f'  id={t[\"id\"]} title={t[\"title\"]} type={t[\"type\"]} created={t[\"created\"]}')
if decay_ready:
    output.append('=== READY TO DECAY (3+ touches) ===')
    for t in decay_ready:
        output.append(f'  id={t[\"id\"]} title={t[\"title\"]} touches={t[\"decay_touches\"]}')
if overdue:
    output.append('=== OVERDUE TODAY ===')
    for t in overdue:
        output.append(f'  id={t[\"id\"]} title={t[\"title\"]} when={t[\"when\"]}')

print('\n'.join(output))
" 2>/dev/null
