#!/usr/bin/env bash
# Pre-hook: check if hourly log has enough entries to consolidate
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
HOURLY_LOG="$MEMORY_DIR/timeline/hourly_log.md"

[ -f "$HOURLY_LOG" ] || exit 0
[ -s "$HOURLY_LOG" ] || exit 0

count=$(grep -c "^### " "$HOURLY_LOG" || true)
count=${count:-0}
[ "$count" -lt 3 ] && exit 0

echo "Hourly log has $count entries. Contents:"
cat "$HOURLY_LOG"
