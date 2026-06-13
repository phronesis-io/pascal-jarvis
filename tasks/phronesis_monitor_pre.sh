#!/usr/bin/env bash
# Pre-hook: fetch recent messages from Phronesis group chat.
# Only triggers during waking hours. Outputs recent messages for analysis.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE_FILE="$JARVIS_DIR/.phronesis_last_ts"

# Identity comes from jarvis.yaml (REQ-50): this is a PUBLIC repo — a real
# Lark chat_id + the owner's open_id do not belong in source. Configure:
#   phronesis:
#     chat_id: oc_xxx
#     user_open_id: ou_xxx
CHAT_ID=$(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$JARVIS_DIR/jarvis.yaml')) or {}
print((cfg.get('phronesis') or {}).get('chat_id', ''))" 2>/dev/null)
PASCAL_ID=$(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$JARVIS_DIR/jarvis.yaml')) or {}
p = (cfg.get('phronesis') or {}).get('user_open_id', '')
print(p or (cfg.get('lark') or {}).get('user_id', ''))" 2>/dev/null)
if [ -z "$CHAT_ID" ]; then
  # Not configured → nothing to monitor (empty pre output = task skipped)
  exit 0
fi

hour=$(date +%H)
if [ "$hour" -lt 9 ] || [ "$hour" -ge 23 ]; then
  exit 0
fi

# Read last check time (ISO 8601)
if [ -f "$STATE_FILE" ]; then
  start_time=$(cat "$STATE_FILE")
else
  # Default: 15 minutes ago
  start_time=$(TZ=Asia/Shanghai date -v-15M +%Y-%m-%dT%H:%M:%S+08:00 2>/dev/null || TZ=Asia/Shanghai date -d '-15 minutes' +%Y-%m-%dT%H:%M:%S+08:00)
fi

end_time=$(TZ=Asia/Shanghai date +%Y-%m-%dT%H:%M:%S+08:00)

# Fetch messages
result=$(lark-cli im +chat-messages-list \
  --chat-id "$CHAT_ID" \
  --start "$start_time" \
  --end "$end_time" \
  --sort "asc" \
  --page-size 50 \
  --as user 2>/dev/null)

# Save current time for next run
echo "$end_time" > "$STATE_FILE"

# Check if we got messages
ok=$(echo "$result" | jq -r '.ok' 2>/dev/null)
if [ "$ok" != "true" ]; then
  exit 0
fi

# Filter: remove Pascal's own messages, extract useful fields
messages=$(echo "$result" | jq -r --arg pid "$PASCAL_ID" '
  .data.messages // []
  | map(select(.sender.id != $pid and .deleted == false))
  | map({
      sender: .sender.name,
      time: .create_time,
      content: .content,
      mentions: ([.mentions[]?.name] | join(", ")),
      reply_to: .reply_to
    })
' 2>/dev/null)

# Count filtered messages
count=$(echo "$messages" | jq 'length' 2>/dev/null || echo "0")
if [ "$count" = "0" ] || [ "$count" = "null" ]; then
  exit 0
fi

# Output context for Claude
cat <<EOF
[PHRONESIS GROUP — Recent Messages]
Chat: Phronesis (公司核心群)
Time window: $start_time → $end_time
Messages ($count new from team):

$messages
EOF
