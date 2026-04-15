#!/usr/bin/env bash
# Pre-hook: check publish cooldown
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

last_pub=$(python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from plugins.eigenflux.client import EigenFluxClient
client = EigenFluxClient('$JARVIS_DIR/eigenflux')
print(client.last_publish_time())
" 2>/dev/null || echo "0")

now=$(date +%s)
settings_file="$JARVIS_DIR/eigenflux/user_settings.json"
cooldown=$(python3 -c "import json; print(json.load(open('$settings_file')).get('publish_cooldown_minutes', 60) * 60)" 2>/dev/null || echo "3600")
elapsed=$(( now - last_pub ))

[ "$elapsed" -lt "$cooldown" ] && exit 0
echo "Ready to publish. Last published ${elapsed}s ago."
