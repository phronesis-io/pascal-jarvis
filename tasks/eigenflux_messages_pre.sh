#!/usr/bin/env bash
# Pre-hook: fetch unread EigenFlux messages via CLI
# Enriches output with entity resolution (matches against known contacts/team)
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=../plugins/eigenflux/client.sh
. "$JARVIS_DIR/plugins/eigenflux/client.sh"

eigenflux_require || exit 0

result=$(eigenflux_msg_fetch 20)
if [ "$result" = "AUTH_REQUIRED" ]; then
  echo "AUTH_REQUIRED: EigenFlux token expired. User needs to run: eigenflux auth login"
  exit 0
fi
[ -z "$result" ] && exit 0

# Extract messages, pipe through entity resolver
echo "$result" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    messages = d.get('messages', [])
    if not messages:
        sys.exit(0)
    print(json.dumps({'messages': messages}))
except Exception:
    sys.exit(0)
" 2>/dev/null | JARVIS_DIR="$JARVIS_DIR" python3 "$JARVIS_DIR/scripts/entity_resolve.py" 2>/dev/null || true
