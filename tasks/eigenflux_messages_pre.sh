#!/usr/bin/env bash
# Pre-hook: fetch unread EigenFlux messages via CLI
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=../plugins/eigenflux/client.sh
. "$JARVIS_DIR/plugins/eigenflux/client.sh"

eigenflux_require || exit 0

result=$(eigenflux_msg_fetch 20)
[ -z "$result" ] && exit 0

# Extract messages, output each as JSON line
echo "$result" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    messages = d.get('messages', [])
    if not messages:
        sys.exit(0)
    for m in messages:
        print(json.dumps(m))
except Exception:
    sys.exit(0)
" 2>/dev/null || true
