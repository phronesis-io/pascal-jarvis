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
files=("$MEMORY_DIR"/hot/*.md "$MEMORY_DIR"/warm/*.md "$MEMORY_DIR"/system/*.md)
shopt -u nullglob
if [ ${#files[@]} -eq 0 ]; then
  exit 0
fi

echo "Current memory files:"
echo "---"
for f in "${files[@]}"; do
  # Show relative path from MEMORY_DIR
  relpath="${f#$MEMORY_DIR/}"
  echo "$relpath"
  head -5 "$f" 2>/dev/null
  echo "---"
done

# --- Cross-session work (other Claude Code sessions) ---
# Pascal works across many sessions/repos simultaneously. That work never appears
# in *this* session's conversation history, so without feeding the digest in full
# here, durable facts (project advanced, new work line, team activity) never reach
# consolidation and the project files go stale. Feed the FULL digest, not head -5.
DIGEST="$MEMORY_DIR/system/cross_session_digest.md"
if [ -f "$DIGEST" ]; then
  echo ""
  echo "=== CROSS-SESSION DIGEST (other sessions — Pascal's primary daytime work) ==="
  cat "$DIGEST"
  echo "=== END CROSS-SESSION DIGEST ==="
fi

# --- Repo activity, last 24h, ALL authors (incl. teammates) ---
# Team work is context too: completeness of situational awareness is priority 1.
# Read-only sweep (no pull — repos-sync already pulls). Captures who shipped what.
REPOS_DIR="${WORK_DIR:-$JARVIS_DIR/..}/repos"
if [ -d "$REPOS_DIR" ]; then
  echo ""
  echo "=== REPO ACTIVITY (last 24h, all authors incl. team) ==="
  for repo in "$REPOS_DIR"/*/; do
    [ -d "$repo/.git" ] || continue
    name=$(basename "$repo")
    log=$(git -C "$repo" log --since="24 hours ago" \
      --pretty=format:'    %ad %an: %s' --date=format:'%m-%d %H:%M' 2>/dev/null)
    [ -z "$log" ] && continue
    echo "  --- $name ---"
    echo "$log"
    echo ""
  done
  echo "=== END REPO ACTIVITY ==="
fi

echo ""
echo "Today's date: $(date '+%Y-%m-%d %A')"
echo "Please review today's conversation history, the cross-session digest, and repo"
echo "activity above, and consolidate new learnings into memory."
