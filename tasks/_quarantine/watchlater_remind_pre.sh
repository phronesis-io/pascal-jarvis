#!/usr/bin/env bash
# Pre-hook: check if there are pending watch-later items worth reminding about.
#
# - Only triggers during waking hours (9-22)
# - Reads watchlater.jsonl for pending items
# - If no pending items, outputs nothing (heartbeat skips)
# - Outputs the pending items list for Claude to pick from

set -euo pipefail

MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
STORE_FILE="$MEMORY_DIR/system/watchlater.jsonl"

# Only during waking hours
hour=$(date +%H)
if [ "$hour" -lt 9 ] || [ "$hour" -ge 22 ]; then
  exit 0
fi

# Check if store exists and has pending items
if [ ! -f "$STORE_FILE" ]; then
  exit 0
fi

pending=$(python3 -c "
import json, sys

store_file = '$STORE_FILE'
entries = []
try:
    with open(store_file, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                e = json.loads(line)
                if e.get('status') == 'pending':
                    entries.append(e)
            except: continue
except FileNotFoundError:
    sys.exit(0)

if not entries:
    sys.exit(0)

print(f'Pending watch-later items ({len(entries)} total):')
print()
for e in entries:
    ts = e.get('ts', '')
    title = e.get('title', '(no title)')
    url = e.get('url', '')
    source = e.get('source', '')
    print(f'- [{ts}] {title}')
    print(f'  URL: {url}')
    print(f'  Source: {source}')
    print()
" 2>/dev/null)

if [ -z "$pending" ]; then
  exit 0
fi

cat <<EOF
Current time: $(date '+%H:%M %A %Y-%m-%d')

$pending
EOF
