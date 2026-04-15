#!/usr/bin/env bash
# Pre-hook: check if user has free time (no calendar events in next hour)
# Only triggers during waking hours (9:00-22:00)
# Requires: lark-cli (for Lark/Feishu calendar) — skip if not available

hour=$(date +%H)
if [ "$hour" -lt 9 ] || [ "$hour" -ge 22 ]; then
  exit 0
fi

now_ts=$(date '+%H:%M')
day=$(date '+%A')

# Try Lark calendar; fall back to always-free if lark-cli not available
if command -v lark-cli &>/dev/null; then
  freebusy=$(lark-cli calendar +freebusy \
    --start "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --end "$(date -v+1H -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -d '+1 hour' -u +%Y-%m-%dT%H:%M:%SZ)" \
    2>/dev/null || true)

  is_free=$(echo "$freebusy" | python3 -c "
import sys, json
from datetime import datetime, timezone
try:
    data = json.load(sys.stdin)
    items = data.get('data') or []
    now = datetime.now(timezone.utc)
    for item in items:
        start = datetime.fromisoformat(item['start_time'])
        end = datetime.fromisoformat(item['end_time'])
        if start <= now < end:
            print('busy')
            sys.exit(0)
except Exception:
    pass
print('free')
" 2>/dev/null || echo "free")

  if [ "$is_free" = "busy" ]; then
    exit 0
  fi
fi

echo "Current time: $now_ts ($day). User is free right now."
