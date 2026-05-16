#!/usr/bin/env bash
# Pre-hook: fetch recent messages from Phronesis group chat.
# Only triggers during waking hours. Outputs recent messages for analysis.

CHAT_ID="oc_REDACTEDREDACTEDREDACTEDREDACTE"
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE_FILE="$JARVIS_DIR/.phronesis_last_ts"
PASCAL_ID="ou_REDACTEDREDACTEDREDACTEDREDACTE"

hour=$(date +%H)
if [ "$hour" -lt 9 ] || [ "$hour" -ge 23 ]; then
  exit 0
fi

# Read last check time (ISO 8601)
if [ -f "$STATE_FILE" ]; then
  start_time=$(cat "$STATE_FILE")
else
  # Default: 15 minutes ago
  start_time=$(date -v-15M -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -d '-15 minutes' -u +%Y-%m-%dT%H:%M:%SZ)
fi

end_time=$(date -u +%Y-%m-%dT%H:%M:%SZ)

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
