#!/usr/bin/env bash
# Pre-hook: only run between 21:00-22:00, provide current memory state
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

hour=$(date +%H)
if [ "$hour" -lt 21 ] || [ "$hour" -ge 22 ]; then
  [ -z "$FORCE" ] && exit 0
fi

echo "Current memory files:"
echo "---"
for f in "$MEMORY_DIR"/*.md; do
  [ -f "$f" ] || continue
  basename "$f"
  head -5 "$f" 2>/dev/null
  echo "---"
done

echo ""
echo "Today's date: $(date '+%Y-%m-%d %A')"
echo "Please review today's conversation history and consolidate new learnings into memory."
