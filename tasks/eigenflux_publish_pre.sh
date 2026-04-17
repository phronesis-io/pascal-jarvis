#!/usr/bin/env bash
# Pre-hook: check publish cooldown via local settings
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export PATH="$HOME/.local/bin:$PATH"

command -v eigenflux >/dev/null 2>&1 || exit 0

settings_file="$JARVIS_DIR/eigenflux/user_settings.json"
now=$(date +%s)

# Check cooldown from local state
last_pub=0
if [ -f "$JARVIS_DIR/eigenflux/publish_state.json" ]; then
  last_pub=$(python3 -c "import json; print(json.load(open('$JARVIS_DIR/eigenflux/publish_state.json')).get('last_publish_epoch', 0))" 2>/dev/null || echo "0")
fi

cooldown=3600
if [ -f "$settings_file" ]; then
  cooldown=$(python3 -c "import json; print(json.load(open('$settings_file')).get('publish_cooldown_minutes', 60) * 60)" 2>/dev/null || echo "3600")
fi

elapsed=$(( now - last_pub ))
[ "$elapsed" -lt "$cooldown" ] && exit 0

echo "Ready to publish. Last published ${elapsed}s ago."
