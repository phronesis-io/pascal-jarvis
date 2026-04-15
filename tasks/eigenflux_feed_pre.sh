#!/usr/bin/env bash
# Pre-hook: fetch EigenFlux feed and persist locally
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

feed=$(python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from plugins.eigenflux.client import EigenFluxClient
import json
client = EigenFluxClient('$JARVIS_DIR/eigenflux')
result = client.pull_feed()  # items auto-persisted by client
items = result.get('data', {}).get('items', [])
if items:
    for item in items:
        print(json.dumps(item))
" 2>/dev/null || true)

[ -z "$feed" ] && exit 0

settings_file="$JARVIS_DIR/eigenflux/user_settings.json"
pref=$(python3 -c "import json; print(json.load(open('$settings_file')).get('feed_delivery_preference','Push everything'))" 2>/dev/null || echo "Push everything")

echo "User delivery preference: $pref"
echo ""
echo "Items:"
echo "$feed"
