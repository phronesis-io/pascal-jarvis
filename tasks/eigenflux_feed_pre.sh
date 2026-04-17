#!/usr/bin/env bash
# Pre-hook: fetch EigenFlux feed via CLI and output items for Claude to triage
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=../plugins/eigenflux/client.sh
. "$JARVIS_DIR/plugins/eigenflux/client.sh"

eigenflux_require || exit 0

feed=$(eigenflux_feed_poll 20)
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
echo "Items:"
echo "$items"
