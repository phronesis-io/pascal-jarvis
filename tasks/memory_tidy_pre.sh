#!/usr/bin/env bash
# Pre-hook: gather memory health metrics for the tidy task.
# Reports file sizes, staleness, and potential issues.
#
# 2026-07-30: sizes are CHARACTERS (wc -m), not bytes (wc -c). The loader
# budgets in core/memory.py are measured in len(text); with CJK-heavy memory
# a byte count runs 2-3x high (behavioral_rules.md: 24643 bytes = 11492
# chars), which made hot/ read as 54k/25k "217% over budget" while the loader
# actually assembled 27.2k against a 30k reserve. A wrong denominator here
# produces a false alarm every cycle, so the units must match the loader's.

MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

[ -d "$MEMORY_DIR" ] || exit 0

# Most six-hour rounds previously sent an identical ~200k memory report to the
# model.  Re-run only when the memory tree changed, plus one daily staleness
# pass so an untouched file can still cross an age threshold.
gate=$(python3 -m core.change_gate tree \
  --root "$MEMORY_DIR" \
  --state "$MEMORY_DIR/system/memory_tidy_change_gate.json" \
  2>/dev/null || echo allow)
[ "$gate" = "allow" ] || exit 0

# Character count in the loader's units. LC_ALL forces UTF-8 so multibyte
# sequences collapse to one char each (matches Python len()).
charcount() { LC_ALL=en_US.UTF-8 wc -m < "$1" | tr -d ' '; }

echo "=== MEMORY HEALTH REPORT ==="
echo "Timestamp: $(date '+%Y-%m-%d %H:%M')"
echo ""

# Exact assembled payloads, in the same Python len() units as the loader.
# Production owner/chat/heartbeat calls use index mode; full is retained only
# as a diagnostic reference for tasks that explicitly opt into it.
MEMORY_DIR="$MEMORY_DIR" python3 - <<'PYTHON' 2>/dev/null || true
import os
from core.memory import MAX_MEMORY_CHARS, load_tiered_memory

root = os.environ["MEMORY_DIR"]
index_chars = len(load_tiered_memory(root, warm_mode="index"))
full_chars = len(load_tiered_memory(root, warm_mode="full"))
print("## Actual assembled payload")
print(f"  PRODUCTION warm=index: {index_chars} / {MAX_MEMORY_CHARS} chars")
print(f"  REFERENCE warm=full: {full_chars} / {MAX_MEMORY_CHARS} chars")
print("")
PYTHON

# Hot files — size check against the loader's hot reserve.
echo "## hot/ (reserve: 30000 chars — core/memory.py HOT_BUDGET; a floor, not a cap)"
hot_total=0
for f in "$MEMORY_DIR"/hot/*.md; do
  [ -f "$f" ] || continue
  chars=$(charcount "$f")
  name=$(basename "$f")
  echo "  $name: ${chars} chars"
  hot_total=$((hot_total + chars))
done
echo "  TOTAL: ${hot_total} chars"
echo ""

# Warm files — list with size and staleness
echo "## warm/ (production=index; expanded files use 11000-char head cap)"
now_epoch=$(date +%s)
for f in "$MEMORY_DIR"/warm/*.md; do
  [ -f "$f" ] || continue
  chars=$(charcount "$f")
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
echo "## timeline/ (reserve: 15000 chars; hourly_archive/daily_archive are NEVER loaded)"
for f in "$MEMORY_DIR"/timeline/*.md; do
  [ -f "$f" ] || continue
  chars=$(charcount "$f")
  entries=$(grep -c "^##\|^###" "$f" 2>/dev/null || echo 0)
  name=$(basename "$f")
  echo "  $name: ${chars} chars, ${entries} entries"
done
echo ""

# System files
echo "## system/ (reserve: 60000 chars; per-file caps in core.memory._SYSTEM_FILE_CAPS)"
for f in "$MEMORY_DIR"/system/*.md "$MEMORY_DIR"/system/*.jsonl; do
  [ -f "$f" ] || continue
  chars=$(charcount "$f")
  name=$(basename "$f")
  echo "  $name: ${chars} chars"
done
echo ""

# Index file
echo "## warm/_index.md"
if [ -f "$MEMORY_DIR/warm/_index.md" ]; then
  chars=$(charcount "$MEMORY_DIR/warm/_index.md")
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
echo "Current warm/_index.md contents:"
cat "$MEMORY_DIR/warm/_index.md" 2>/dev/null || echo "(missing)"
