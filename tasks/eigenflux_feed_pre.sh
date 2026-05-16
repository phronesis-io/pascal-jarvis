#!/usr/bin/env bash
# Pre-hook: fetch EigenFlux feed via CLI and output items for Claude to triage
# Respects dynamic poll interval from eigenflux config (default 600s).
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=../plugins/eigenflux/client.sh
. "$JARVIS_DIR/plugins/eigenflux/client.sh"

eigenflux_require || exit 0

# Dynamic poll interval: read from eigenflux config, enforce locally
# (HEARTBEAT.md interval is a minimum; this script adds its own throttle)
_poll_state="$JARVIS_DIR/eigenflux/.feed_poll_state"
_now=$(date +%s)
_poll_interval=$(eigenflux config get --key feed_poll_interval 2>/dev/null | tr -d '"' || echo "600")
_poll_interval=${_poll_interval:-600}
# Clamp to 10-86400
[ "$_poll_interval" -lt 10 ] 2>/dev/null && _poll_interval=10
[ "$_poll_interval" -gt 86400 ] 2>/dev/null && _poll_interval=86400

if [ -f "$_poll_state" ]; then
  _last_poll=$(cat "$_poll_state" 2>/dev/null || echo 0)
  _elapsed=$((_now - _last_poll))
  if [ "$_elapsed" -lt "$_poll_interval" ]; then
    exit 0  # not time yet
  fi
fi
echo "$_now" > "$_poll_state"

feed=$(eigenflux_feed_poll 20)
if [ "$feed" = "AUTH_REQUIRED" ]; then
  echo "AUTH_REQUIRED: EigenFlux token expired. User needs to run: eigenflux auth login"
  exit 0
fi
[ -z "$feed" ] && exit 0

items=$(echo "$feed" | python3 -c "
import json, sys
d = json.load(sys.stdin)
items = d.get('items', [])
if not items:
    sys.exit(0)
for item in items:
    print(json.dumps(item))
" 2>/dev/null || true)

[ -z "$items" ] && exit 0

settings_file="$JARVIS_DIR/eigenflux/user_settings.json"
pref="Push everything"
if [ -f "$settings_file" ]; then
  pref=$(python3 -c "import json; print(json.load(open('$settings_file')).get('feed_delivery_preference','Push everything'))" 2>/dev/null || echo "Push everything")
fi

echo "User delivery preference: $pref"
echo ""

# Enrich each item with full details (URL, content) via feed get
echo "Items (enriched with full details):"
echo "$items" | while IFS= read -r item_line; do
  [ -z "$item_line" ] && continue
  item_id=$(echo "$item_line" | python3 -c "import json,sys; print(json.load(sys.stdin).get('item_id',''))" 2>/dev/null || true)
  if [ -n "$item_id" ]; then
    # Fetch full item detail (content + url)
    full_detail=$(eigenflux_feed_get "$item_id" 2>/dev/null || true)
    if [ -n "$full_detail" ]; then
      # Merge full detail into the item
      merged=$(JV_ITEM="$item_line" JV_DETAIL="$full_detail" python3 -c "
import json, os, sys
try:
    item = json.loads(os.environ['JV_ITEM'])
    detail = json.loads(os.environ['JV_DETAIL'])
    full = detail.get('data', {}).get('item', detail.get('data', {}))
    if full:
        # Add URL and full content if not already present
        if full.get('url') and not item.get('url'):
            item['url'] = full['url']
        if full.get('source_url') and not item.get('source_url'):
            item['source_url'] = full['source_url']
        if full.get('content') and len(full['content']) > len(item.get('content', '')):
            item['full_content'] = full['content'][:3000]
        if full.get('notes'):
            item['notes_detail'] = full['notes'] if isinstance(full['notes'], dict) else full['notes']
    print(json.dumps(item, ensure_ascii=False))
except Exception:
    print(os.environ['JV_ITEM'])
" 2>/dev/null || echo "$item_line")
      echo "$merged"
    else
      echo "$item_line"
    fi
  else
    echo "$item_line"
  fi
done
