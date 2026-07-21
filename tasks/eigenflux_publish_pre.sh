#!/usr/bin/env bash
# Pre-hook: check publish cooldown via local settings
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export PATH="$HOME/.local/bin:$PATH"

command -v eigenflux >/dev/null 2>&1 || exit 0

settings_file="$JARVIS_DIR/eigenflux/user_settings.json"
now=$(date +%s)

# Check cooldown from local state
last_pub=0
if [ -f "$JARVIS_DIR/eigenflux/publish_state.json" ]; then
  last_pub=$(python3 -c "import json; print(json.load(open('$JARVIS_DIR/eigenflux/publish_state.json')).get('last_publish_epoch', 0))" 2>/dev/null || echo "0")
fi

cooldown=7200  # 2h default — all broadcasts go through user confirmation, over-publishing impossible
if [ -f "$settings_file" ]; then
  cooldown=$(python3 -c "import json; print(json.load(open('$settings_file')).get('publish_cooldown_minutes', 60) * 60)" 2>/dev/null || echo "3600")
fi

elapsed=$(( now - last_pub ))
[ "$elapsed" -lt "$cooldown" ] && exit 0

echo "Ready to publish. Last published ${elapsed}s ago."

# Show recent publish history so Claude avoids duplicate topics
if [ -f "$JARVIS_DIR/eigenflux/publish_state.json" ]; then
  recent=$(python3 -c "
import json, sys
from datetime import datetime
try:
    state = json.load(open('$JARVIS_DIR/eigenflux/publish_state.json'))
    history = state.get('recent', [])
    if not history:
        sys.exit(0)
    print()
    print('=== RECENT BROADCASTS (do NOT repeat these topics) ===')
    for item in history[-10:]:
        dt = datetime.fromtimestamp(item['epoch']).strftime('%m/%d %H:%M')
        preview = item.get('content_preview', item.get('summary', ''))
        print(f'  {dt}: {preview}')
except Exception:
    pass
" 2>/dev/null)
  [ -n "$recent" ] && echo "$recent"
fi

# Material pool: recent memory highlights for content inspiration
MEMORY_DIR="$HOME/.claude/projects/-Users-pascal-Desktop-jarvis/memory"
if [ -d "$MEMORY_DIR" ]; then
  material=$(python3 -c "
import sys
from pathlib import Path
from datetime import datetime, timedelta

mem_dir = Path('$MEMORY_DIR')
cutoff = datetime.now().timestamp() - 7 * 86400  # last 7 days
entries = []
for f in mem_dir.glob('*.md'):
    if f.name == 'MEMORY.md':
        continue
    if f.stat().st_mtime < cutoff:
        continue
    first_line = f.read_text(errors='ignore').split('\n')[0].strip()
    if first_line.startswith('---'):
        lines = f.read_text(errors='ignore').split('\n')
        for line in lines:
            if line.startswith('description:'):
                first_line = line.split(':', 1)[1].strip()
                break
    entries.append((f.stat().st_mtime, first_line[:120]))

if entries:
    entries.sort(reverse=True)
    print()
    print('=== RECENT WORK & INSIGHTS (inspiration for broadcasts) ===')
    for _, desc in entries[:8]:
        print(f'  - {desc}')
" 2>/dev/null)
  [ -n "$material" ] && echo "$material"
fi

# Recent git commits across key repos (last 7 days)
commits=$(python3 -c "
import subprocess, os
repos = [
    ('jarvis', os.path.expanduser('~/Desktop/jarvis/repos/pascal-jarvis')),
    ('eigenflux', os.path.expanduser('~/Desktop/jarvis/repos/eigenflux')),
    ('pgc', os.path.expanduser('~/Desktop/jarvis/repos/eigenflux-pgc')),
]
lines = []
for label, path in repos:
    if not os.path.isdir(path):
        continue
    try:
        r = subprocess.run(
            ['git', 'log', '--oneline', '--since=7 days ago', '-10', '--no-merges'],
            capture_output=True, text=True, cwd=path, timeout=5)
        for line in (r.stdout or '').strip().split('\n'):
            if line.strip():
                lines.append(f'  [{label}] {line.strip()[:100]}')
    except Exception:
        pass

if lines:
    print()
    print('=== RECENT COMMITS (what Pascal has been building) ===')
    for l in lines[:15]:
        print(l)
" 2>/dev/null)
[ -n "$commits" ] && echo "$commits"
