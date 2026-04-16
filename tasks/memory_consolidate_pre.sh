#!/usr/bin/env bash
# Pre-hook: only run between 21:00-22:00, provide current memory state
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
FORCE="${FORCE:-}"

if [ ! -d "$MEMORY_DIR" ]; then
  echo "[memory-consolidate] MEMORY_DIR not found: $MEMORY_DIR" >&2
  exit 0
fi

hour=$(date +%H)
if [ "$hour" -lt 21 ] || [ "$hour" -ge 22 ]; then
  [ -z "$FORCE" ] && exit 0
fi

shopt -s nullglob
files=("$MEMORY_DIR"/*.md)
shopt -u nullglob
if [ ${#files[@]} -eq 0 ]; then
  exit 0
fi

echo "Current memory files:"
echo "---"
for f in "${files[@]}"; do
  basename "$f"
  head -5 "$f" 2>/dev/null
  echo "---"
done

echo ""
echo "Today's date: $(date '+%Y-%m-%d %A')"
echo "Please review today's conversation history and consolidate new learnings into memory."
