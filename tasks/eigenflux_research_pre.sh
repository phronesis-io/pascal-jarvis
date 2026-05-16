#!/usr/bin/env bash
# Pre-hook: read needs_research queue, fetch item details via CLI, gather context
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=../plugins/eigenflux/client.sh
. "$JARVIS_DIR/plugins/eigenflux/client.sh"

eigenflux_require || exit 0

QUEUE="$JARVIS_DIR/eigenflux/needs_research.jsonl"
REPOS_DIR="$JARVIS_DIR/repos"

# Nothing to research?
[ ! -f "$QUEUE" ] && exit 0
[ ! -s "$QUEUE" ] && exit 0

count=$(wc -l < "$QUEUE" | tr -d ' ')
[ "$count" -eq 0 ] && exit 0

echo "=== RESEARCH QUEUE ($count items) ==="
echo ""

# Pull all repos first (silent, best-effort)
for repo in "$REPOS_DIR"/*/; do
  [ -d "$repo/.git" ] || continue
  git -C "$repo" pull --ff-only 2>/dev/null || true
done

# For each queued item, fetch full details via CLI
while IFS= read -r line; do
  [ -z "$line" ] && continue
  item_id=$(echo "$line" | python3 -c "import json,sys; print(json.load(sys.stdin).get('item_id',''))" 2>/dev/null || true)
  [ -z "$item_id" ] && continue

  score=$(echo "$line" | python3 -c "import json,sys; print(json.load(sys.stdin).get('score','?'))" 2>/dev/null || echo "?")
  reason=$(echo "$line" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason','?'))" 2>/dev/null || echo "?")
  queued_at=$(echo "$line" | python3 -c "import json,sys; print(json.load(sys.stdin).get('queued_at','?'))" 2>/dev/null || echo "?")

  echo "--- ITEM $item_id ---"
  echo "Queued: $queued_at"
  echo "Score: $score"
  echo "Triage reason: $reason"
  echo ""

  # Fetch full item detail via CLI
  full_detail=$(eigenflux_feed_get "$item_id" 2>/dev/null || true)
  if [ -n "$full_detail" ]; then
    JV_DETAIL="$full_detail" python3 -c "
import json, os, sys
try:
    detail = json.loads(os.environ['JV_DETAIL'])
    item = detail.get('data', {}).get('item', detail.get('data', {}))
    if not item:
        sys.exit(0)
    content = item.get('content', '')
    url = item.get('url', '') or item.get('source_url', '')
    notes_raw = item.get('notes', '')
    notes = {}
    if isinstance(notes_raw, str) and notes_raw.strip():
        try: notes = json.loads(notes_raw)
        except: notes = {'raw': notes_raw}
    elif isinstance(notes_raw, dict):
        notes = notes_raw
    print(f'Content: {content[:3000]}')
    if url: print(f'URL: {url}')
    if notes: print(f'Notes: {json.dumps(notes, ensure_ascii=False)}')
    print(f'Author: {item.get(\"agent_name\", \"?\")} (agent_id: {item.get(\"agent_id\", \"?\")})')
except Exception as e:
    print(f'[detail parse error: {e}]')
" 2>/dev/null || echo "[detail fetch failed]"
  else
    echo "[no detail available]"
  fi
  echo ""

done < "$QUEUE"

# List available repos for reference
echo ""
echo "=== AVAILABLE REPOS ==="
for repo in "$REPOS_DIR"/*/; do
  [ -d "$repo/.git" ] || continue
  name=$(basename "$repo")
  branch=$(git -C "$repo" branch --show-current 2>/dev/null || echo "?")
  echo "  $name ($branch)"
done
