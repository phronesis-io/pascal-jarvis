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

cooldown=14400  # 4h default (was 1h — reduced from 3.3/day to ~1/day based on 6% engagement)
if [ -f "$settings_file" ]; then
  cooldown=$(python3 -c "import json; print(json.load(open('$settings_file')).get('publish_cooldown_minutes', 60) * 60)" 2>/dev/null || echo "3600")
fi

elapsed=$(( now - last_pub ))
[ "$elapsed" -lt "$cooldown" ] && exit 0

echo "Ready to publish. Last published ${elapsed}s ago."

# Show recent publish history so Claude avoids duplicate topics
if [ -f "$JARVIS_DIR/eigenflux/publish_state.json" ]; then
  recent=$(python3 -c "
import json, sys
from datetime import datetime
try:
    state = json.load(open('$JARVIS_DIR/eigenflux/publish_state.json'))
    history = state.get('recent', [])
    if not history:
        sys.exit(0)
    print()
    print('=== RECENT BROADCASTS (do NOT repeat these topics) ===')
    for item in history[-10:]:
        dt = datetime.fromtimestamp(item['epoch']).strftime('%m/%d %H:%M')
        preview = item.get('content_preview', item.get('summary', ''))
        print(f'  {dt}: {preview}')
except Exception:
    pass
" 2>/dev/null)
  [ -n "$recent" ] && echo "$recent"
fi
