#!/usr/bin/env bash
# Pre-hook: check if daily log has 5+ days to consolidate
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
DAILY_LOG="$MEMORY_DIR/daily_log.md"
LONGTERM="$MEMORY_DIR/longterm_digest.md"

[ -f "$DAILY_LOG" ] || exit 0
[ -s "$DAILY_LOG" ] || exit 0

count=$(grep -c "^## " "$DAILY_LOG" || true)
count=${count:-0}
[ "$count" -lt 5 ] && exit 0

echo "Daily log has $count entries (>=5), ready for consolidation."
echo ""
echo "=== DAILY LOG ==="
cat "$DAILY_LOG"
echo ""
if [ -f "$LONGTERM" ]; then
  echo "=== EXISTING LONG-TERM DIGEST ==="
  cat "$LONGTERM"
fi
