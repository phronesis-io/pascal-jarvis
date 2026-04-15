#!/usr/bin/env bash
# Pre-hook: check if weekly digest is long enough to compress
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
WEEKLY_DIGEST="$MEMORY_DIR/longterm_digest.md"
MONTHLY_ARCHIVE="$MEMORY_DIR/monthly_archive.md"

[ -f "$WEEKLY_DIGEST" ] || exit 0
[ -s "$WEEKLY_DIGEST" ] || exit 0

wc=$(wc -w < "$WEEKLY_DIGEST" || echo 0)
wc=$(echo $wc | tr -d ' ')
wc=${wc:-0}
[ "$wc" -lt 200 ] && exit 0

echo "Weekly digest has ${wc} words, ready for monthly compression."
echo ""
echo "=== WEEKLY DIGEST ==="
cat "$WEEKLY_DIGEST"
echo ""
if [ -f "$MONTHLY_ARCHIVE" ]; then
  echo "=== EXISTING MONTHLY ARCHIVE ==="
  cat "$MONTHLY_ARCHIVE"
fi
