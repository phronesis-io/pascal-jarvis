#!/usr/bin/env bash
# Pre-hook: gather memory health metrics for the tidy task.
# Reports file sizes, staleness, and potential issues.

MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

[ -d "$MEMORY_DIR" ] || exit 0

echo "=== MEMORY HEALTH REPORT ==="
echo "Timestamp: $(date '+%Y-%m-%d %H:%M')"
echo ""

# Hot files — size check (budget: 6K total)
echo "## hot/ (budget: 25000 chars — core/memory.py HOT_BUDGET)"
hot_total=0
for f in "$MEMORY_DIR"/hot/*.md; do
  [ -f "$f" ] || continue
  chars=$(wc -c < "$f" | tr -d ' ')
  name=$(basename "$f")
  echo "  $name: ${chars} chars"
  hot_total=$((hot_total + chars))
done
echo "  TOTAL: ${hot_total} chars"
echo ""

# Warm files — list with size and staleness
echo "## warm/"
now_epoch=$(date +%s)
for f in "$MEMORY_DIR"/warm/*.md; do
  [ -f "$f" ] || continue
  chars=$(wc -c < "$f" | tr -d ' ')
  name=$(basename "$f")
  mod_epoch=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)
  if [ -n "$mod_epoch" ]; then
    stale_days=$(( (now_epoch - mod_epoch) / 86400 ))
    stale_tag=""
    [ "$stale_days" -ge 30 ] && stale_tag=" [STALE ${stale_days}d]"
    echo "  $name: ${chars} chars, modified ${stale_days}d ago${stale_tag}"
  else
    echo "  $name: ${chars} chars"
  fi
done
echo ""

# Timeline files — size + entry counts
echo "## timeline/"
for f in "$MEMORY_DIR"/timeline/*.md; do
  [ -f "$f" ] || continue
  chars=$(wc -c < "$f" | tr -d ' ')
  entries=$(grep -c "^##\|^###" "$f" 2>/dev/null || echo 0)
  name=$(basename "$f")
  echo "  $name: ${chars} chars, ${entries} entries"
done
echo ""

# System files
echo "## system/"
for f in "$MEMORY_DIR"/system/*.md "$MEMORY_DIR"/system/*.jsonl; do
  [ -f "$f" ] || continue
  chars=$(wc -c < "$f" | tr -d ' ')
  name=$(basename "$f")
  echo "  $name: ${chars} chars"
done
echo ""

# Index file
echo "## _index.md"
if [ -f "$MEMORY_DIR/_index.md" ]; then
  chars=$(wc -c < "$MEMORY_DIR/_index.md" | tr -d ' ')
  echo "  ${chars} chars"
else
  echo "  MISSING — needs regeneration"
fi
echo ""

# Duplicate detection in timeline
echo "## Potential issues"
hourly="$MEMORY_DIR/timeline/hourly_log.md"
if [ -f "$hourly" ]; then
  dupes=$(grep "^### " "$hourly" | sort | uniq -d | head -5)
  if [ -n "$dupes" ]; then
    echo "  Duplicate hourly entries:"
    echo "$dupes" | sed 's/^/    /'
  fi
fi

daily="$MEMORY_DIR/timeline/daily_log.md"
if [ -f "$daily" ]; then
  dupes=$(grep "^## " "$daily" | sort | uniq -d | head -5)
  if [ -n "$dupes" ]; then
    echo "  Duplicate daily entries:"
    echo "$dupes" | sed 's/^/    /'
  fi
fi

echo ""
echo "Current _index.md contents:"
cat "$MEMORY_DIR/_index.md" 2>/dev/null || echo "(missing)"
