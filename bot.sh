#!/usr/bin/env bash
# jarvis-harness: Lark/Feishu bot + heartbeat-driven personal AI agent
#
# - Listens for user messages on Lark
# - Heartbeat loop runs scheduled tasks (feed triage, memory consolidation, etc.)
# - Claude Code handles conversation with full memory injection
#
# Configuration: jarvis.yaml (copy from jarvis.example.yaml)

set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
export JARVIS_DIR

# ── Load config ───────────────────────────────────────────────────────
CONFIG_FILE="$JARVIS_DIR/jarvis.yaml"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: jarvis.yaml not found. Copy jarvis.example.yaml and configure it." >&2
  exit 1
fi

# Parse YAML config (lightweight — only extract what shell needs)
_yaml_val() { python3 -c "import yaml; c=yaml.safe_load(open('$CONFIG_FILE')); print($1)" 2>/dev/null; }

USER_ID=$(_yaml_val "c.get('lark',{}).get('user_id','')")
DATA_DIR=$(_yaml_val "import os; print(os.path.expanduser(c.get('data_dir','~/.jarvis')))")
MAX_SESSION_SIZE=$(_yaml_val "c.get('claude',{}).get('max_session_size',512000)")
HEARTBEAT_MODEL=$(_yaml_val "c.get('claude',{}).get('heartbeat_model','sonnet')")
CHECK_INTERVAL=$(_yaml_val "c.get('heartbeat',{}).get('check_interval',10)")

MEMORY_DIR="$DATA_DIR/memory"
SESSION_DIR="$DATA_DIR/sessions"
SESSION_TRACKER="$JARVIS_DIR/active_sessions.json"
HEARTBEAT_TRIGGER="/tmp/jarvis-heartbeat-trigger"

export MEMORY_DIR SESSION_DIR

echo "Starting jarvis-harness..." >&2
echo "  JARVIS_DIR: $JARVIS_DIR" >&2
echo "  DATA_DIR:   $DATA_DIR" >&2
echo "  USER_ID:    ${USER_ID:-(not set, IM disabled)}" >&2

# Ensure directories exist
mkdir -p "$DATA_DIR" "$MEMORY_DIR" "$SESSION_DIR" "$JARVIS_DIR/eigenflux"

# Initialize tracker
[ -f "$SESSION_TRACKER" ] || echo "{}" > "$SESSION_TRACKER"

# ── Session management ────────────────────────────────────────────────
get_session_id() {
  local conv_key="$1"
  python3 - "$SESSION_TRACKER" "$SESSION_DIR" "$MAX_SESSION_SIZE" "$conv_key" << 'PYEOF'
import json, uuid, os, sys

tracker_path = sys.argv[1]
session_dir = sys.argv[2]
max_size = int(sys.argv[3])
conv_key = sys.argv[4]

tracker = json.load(open(tracker_path))
entry = tracker.get(conv_key, {})
sid = entry.get('session_id', '')
session_file = os.path.join(session_dir, f'{sid}.jsonl') if sid else ''

needs_new = False
if not sid:
    needs_new = True
elif os.path.exists(session_file) and os.path.getsize(session_file) > max_size:
    needs_new = True
    print("ROTATED", file=sys.stderr)

if needs_new:
    counter = entry.get('counter', 0) + 1
    sid = str(uuid.uuid5(uuid.UUID('a1b2c3d4-e5f6-7890-abcd-ef1234567890'), f'{conv_key}-{counter}'))
    tracker[conv_key] = {'session_id': sid, 'counter': counter}
    json.dump(tracker, open(tracker_path, 'w'), indent=2)

print(sid)
PYEOF
}

# ── Memory loader ─────────────────────────────────────────────────────
load_memory() {
  python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.memory import load_tiered_memory
print(load_tiered_memory('$MEMORY_DIR'))
" 2>/dev/null || true
}

# ── Send to Lark ──────────────────────────────────────────────────────
send_to_lark() {
  local content="$1"
  [ -z "$content" ] && return
  [ -z "$USER_ID" ] && return
  lark-cli im +messages-send \
    --user-id "$USER_ID" \
    --markdown "$content" \
    --as bot 2>/dev/null || true
}

# ── Heartbeat Loop (background) ──────────────────────────────────────
heartbeat_loop() {
  sleep 3
  echo "[$(date '+%H:%M:%S')] [heartbeat] Starting (${CHECK_INTERVAL}s cycle)..." >&2

  while true; do
    force_flag=""
    if [ -f "$HEARTBEAT_TRIGGER" ]; then
      force_flag="--force"
      rm -f "$HEARTBEAT_TRIGGER"
    fi

    output=$(python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.heartbeat import HeartbeatRunner
runner = HeartbeatRunner(
    jarvis_dir='$JARVIS_DIR',
    heartbeat_file='$JARVIS_DIR/HEARTBEAT.md',
    state_file='$JARVIS_DIR/heartbeat_state.json',
    memory_dir='$MEMORY_DIR',
    model='$HEARTBEAT_MODEL',
)
result = runner.run_cycle($( [ -n "$force_flag" ] && echo "force=True" || echo ""))
if result:
    print(result)
" 2>/dev/null || true)

    if [ -n "$output" ]; then
      send_to_lark "$output"
      echo "[$(date '+%H:%M:%S')] [heartbeat] Beat sent" >&2
    fi

    sleep "$CHECK_INTERVAL"
  done
}

heartbeat_loop &
HEARTBEAT_PID=$!
echo "[$(date '+%H:%M:%S')] Heartbeat started (PID: $HEARTBEAT_PID)" >&2

# ── Lark Message Listener (foreground) ────────────────────────────────
if [ -z "$USER_ID" ]; then
  echo "[$(date '+%H:%M:%S')] No Lark user_id configured. Running heartbeat-only mode." >&2
  echo "  Press Ctrl+C to stop." >&2
  wait $HEARTBEAT_PID
  exit 0
fi

lark-cli event +subscribe \
  --event-types im.message.receive_v1 --compact --quiet --as bot \
  | while IFS= read -r line; do
      content=$(echo "$line" | jq -r '.content // empty' 2>/dev/null)
      message_id=$(echo "$line" | jq -r '.message_id // empty' 2>/dev/null)
      chat_type=$(echo "$line" | jq -r '.chat_type // empty' 2>/dev/null)
      chat_id=$(echo "$line" | jq -r '.chat_id // empty' 2>/dev/null)
      sender_id=$(echo "$line" | jq -r '.sender_id // empty' 2>/dev/null)

      [ -z "$content" ] || [ -z "$message_id" ] && continue

      # Handle manual heartbeat trigger
      content_lower=$(echo "$content" | tr '[:upper:]' '[:lower:]')
      if [ "$content_lower" = "loop" ] || [ "$content_lower" = "heartbeat" ]; then
        touch "$HEARTBEAT_TRIGGER"
        lark-cli im +messages-reply \
          --message-id "$message_id" \
          --text "Heartbeat triggered" \
          --as bot 2>/dev/null || true
        continue
      fi

      # Determine session
      if [ "$chat_type" = "p2p" ]; then
        conv_key="$sender_id"
      else
        conv_key="$chat_id"
      fi
      session_result=$(get_session_id "$conv_key" 2>&1)
      session_id=$(echo "$session_result" | tail -1)
      rotated=$(echo "$session_result" | grep ROTATED || true)

      if [ -n "$rotated" ]; then
        echo "[$(date '+%H:%M:%S')] Session rotated for $conv_key" >&2
        python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.heartbeat import HeartbeatRunner
runner = HeartbeatRunner('$JARVIS_DIR', '$JARVIS_DIR/HEARTBEAT.md',
    '$JARVIS_DIR/heartbeat_state.json', '$MEMORY_DIR', '$HEARTBEAT_MODEL')
runner.run_cycle(force=True, only_task='memory-hourly')
" 2>/dev/null || true
      fi

      echo "[$(date '+%H:%M:%S')] [$session_id] Received: $content" >&2

      # Send "Thinking..." indicator
      thinking_result=$(lark-cli im +messages-reply \
        --message-id "$message_id" \
        --text "Thinking..." \
        --as bot 2>/dev/null || true)
      thinking_id=$(echo "$thinking_result" | jq -r '.data.message_id // empty' 2>/dev/null || true)

      # Build system prompt with memory + recent turns
      memory=$(load_memory)
      now_ts=$(date '+%Y-%m-%d %H:%M %A')

      counter=$(python3 -c "import json; print(json.load(open('$SESSION_TRACKER')).get('$conv_key',{}).get('counter',0))" 2>/dev/null || echo 0)
      recent_turns=$(python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.session import build_recent_turns
print(build_recent_turns('$SESSION_DIR', '$session_id', $counter, '$conv_key', 20))
" 2>/dev/null || true)

      sys_prompt="You are a personal assistant and life mentor. Reply in the same language the user uses.
Current time: $now_ts

$memory

$recent_turns"

      # Call Claude
      session_file="$SESSION_DIR/${session_id}.jsonl"
      if [ -f "$session_file" ]; then
        answer=$(claude -p "$content" \
          --resume "$session_id" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          < /dev/null 2>/dev/null || true)
      else
        answer=$(claude -p "$content" \
          --session-id "$session_id" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          < /dev/null 2>/dev/null || true)
      fi

      if [ -z "$answer" ]; then
        continue
      fi

      # Delete "Thinking..." and send reply
      if [ -n "$thinking_id" ]; then
        lark-cli im messages delete --params "{\"message_id\":\"$thinking_id\"}" --as bot 2>/dev/null || true
      fi

      lark-cli im +messages-reply \
        --message-id "$message_id" \
        --markdown "$answer" \
        --as bot 2>/dev/null || true
      echo "[$(date '+%H:%M:%S')] Replied" >&2
    done

# Cleanup
kill $HEARTBEAT_PID 2>/dev/null
