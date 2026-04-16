#!/usr/bin/env bash
# jarvis-harness: Lark/Feishu bot + heartbeat-driven personal AI agent
#
# - Listens for user messages on Lark
# - Heartbeat loop runs scheduled tasks (feed triage, memory consolidation, etc.)
# - Claude Code handles conversation with full memory injection
#
# Configuration: jarvis.yaml (copy from jarvis.example.yaml)

set -uo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
export JARVIS_DIR

LOG_FILE="$JARVIS_DIR/jarvis.log"
MEMORY_CACHE_FILE="$JARVIS_DIR/.memory_cache"   # last-known-good memory snapshot

# ── Logging ──────────────────────────────────────────────────────────
# All log messages go to jarvis.log AND stderr. They NEVER go to stdout,
# which is reserved for user-facing output that may be sent to Lark.
log() {
  local level="$1"; shift
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*"
  echo "$msg" >&2
  echo "$msg" >> "$LOG_FILE"
}

log_err()  { log ERROR "$@"; }
log_warn() { log WARN  "$@"; }
log_info() { log INFO  "$@"; }

# ── Portable timeout (macOS has no 'timeout' by default) ─────────────
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD="gtimeout"
else
  TIMEOUT_CMD=""
fi

# Run a command with a timeout. Usage: with_timeout <seconds> <cmd> <args...>
# On macOS without coreutils, falls back to no timeout (Python layer handles it).
with_timeout() {
  local secs="$1"; shift
  if [ -n "$TIMEOUT_CMD" ]; then
    "$TIMEOUT_CMD" "$secs" "$@"
  else
    "$@"
  fi
}

# ── Prerequisite check ──────────────────────────────────────────────
# Fail early with actionable errors instead of cryptic failures deep in
# the Python eval or lark-cli subshell.
missing_dep() {
  echo "ERROR: required dependency not found: $1" >&2
  echo "       install: $2" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || missing_dep "python3" "brew install python3 (macOS) / apt install python3 (Linux)"
command -v jq      >/dev/null 2>&1 || missing_dep "jq"      "brew install jq (macOS) / apt install jq (Linux)"
python3 -c "import yaml" >/dev/null 2>&1 || missing_dep "python3 yaml module" \
  "pip install pyyaml    (or run: ./setup.sh)"

# ── Load config via core/config.py (single source of truth) ──────────
# Uses shlex.quote() so config values are never shell-interpolated.
CONFIG_FILE="$JARVIS_DIR/jarvis.yaml"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: jarvis.yaml not found." >&2
  echo "       Run: cp jarvis.example.yaml jarvis.yaml && \$EDITOR jarvis.yaml" >&2
  echo "       Or:  ./setup.sh   (does it + more)" >&2
  exit 1
fi

CONFIG_VARS=$(JARVIS_DIR="$JARVIS_DIR" CONFIG_FILE="$CONFIG_FILE" python3 - <<'PYEOF'
import os, shlex, sys
sys.path.insert(0, os.environ["JARVIS_DIR"])
from core.config import Config
c = Config(os.environ["CONFIG_FILE"])
def emit(name, value):
    print(f"{name}={shlex.quote(str(value))}")
emit("USER_ID", c.lark.get("user_id", ""))
emit("DATA_DIR", c.data_dir)
emit("WORK_DIR", c.work_dir)
emit("MEMORY_DIR", c.memory_dir)
emit("MAX_SESSION_SIZE", c.claude.get("max_session_size", 512000))
emit("HEARTBEAT_MODEL", c.claude.get("heartbeat_model", "sonnet"))
emit("CHECK_INTERVAL", c.heartbeat.get("check_interval", 10))
emit("ADMIN_ENABLED", str(bool(c.admin.get("enabled", False))).lower())
emit("ADMIN_HOST", c.admin.get("host", "127.0.0.1"))
emit("ADMIN_PORT", c.admin.get("port", 3456))
PYEOF
)
# shellcheck disable=SC1090
eval "$CONFIG_VARS"

# Claude Code stores sessions in ~/.claude/projects/<slug>/, where <slug>
# is the absolute cwd with every '/' replaced by '-' (leading dash kept).
CLAUDE_PROJECT_DIR="$HOME/.claude/projects/$(echo "$WORK_DIR" | sed 's|/|-|g')"
SESSION_TRACKER="$JARVIS_DIR/active_sessions.json"
HEARTBEAT_TRIGGER="/tmp/jarvis-heartbeat-trigger"

export MEMORY_DIR WORK_DIR CLAUDE_PROJECT_DIR

log_info "Starting jarvis-harness..."
log_info "  JARVIS_DIR: $JARVIS_DIR"
log_info "  DATA_DIR:   $DATA_DIR"
log_info "  WORK_DIR:   $WORK_DIR"
log_info "  CLAUDE_DIR: $CLAUDE_PROJECT_DIR"
log_info "  USER_ID:    ${USER_ID:-(not set, IM disabled)}"
log_info "  TIMEOUT:    ${TIMEOUT_CMD:-(unavailable, Python layer handles)}"

# Lark is configured but lark-cli missing → log a warning but don't abort.
# The bot will still run heartbeat-only.
if [ -n "$USER_ID" ] && ! command -v lark-cli >/dev/null 2>&1; then
  log_warn "lark.user_id is set but lark-cli is not installed (install: npm i -g @larksuite/cli)"
  log_warn "Bot will run in heartbeat-only mode. Configure lark-cli or remove lark.user_id."
  USER_ID=""  # degrade to headless
fi

mkdir -p "$DATA_DIR" "$MEMORY_DIR" "$JARVIS_DIR/eigenflux"
[ -f "$SESSION_TRACKER" ] || echo "{}" > "$SESSION_TRACKER"

# ── Load built-in plugins ────────────────────────────────────────────
# Lark (Feishu) — shell helpers for IM. Other plugins (e.g. eigenflux)
# are Python libraries loaded directly by tasks/*.py scripts.
# shellcheck source=plugins/lark/client.sh
. "$JARVIS_DIR/plugins/lark/client.sh"

# ── Session management ────────────────────────────────────────────────
# Passes conv_key via env to avoid shell-injection into the Python source.
get_session_id() {
  local conv_key="$1"
  JV_TRACKER="$SESSION_TRACKER" JV_SDIR="$CLAUDE_PROJECT_DIR" \
    JV_MAX="$MAX_SESSION_SIZE" JV_KEY="$conv_key" python3 <<'PYEOF'
import json, uuid, os, sys

tracker_path = os.environ["JV_TRACKER"]
session_dir  = os.environ["JV_SDIR"]
max_size     = int(os.environ["JV_MAX"])
conv_key     = os.environ["JV_KEY"]

try:
    tracker = json.load(open(tracker_path))
except Exception:
    tracker = {}

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
    with open(tracker_path, 'w') as f:
        json.dump(tracker, f, indent=2)

print(sid)
PYEOF
}

# ── Memory loader with last-known-good cache ─────────────────────────
# If Python fails, we reuse the cached snapshot so Claude never gets an
# empty memory string (which causes the bot to "forget" everything).
load_memory() {
  local fresh
  fresh=$(python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.memory import load_tiered_memory
print(load_tiered_memory('$MEMORY_DIR'))
" 2>>"$LOG_FILE")

  if [ -n "$fresh" ]; then
    printf '%s' "$fresh" > "$MEMORY_CACHE_FILE"
    printf '%s' "$fresh"
  elif [ -s "$MEMORY_CACHE_FILE" ]; then
    log_warn "Memory load returned empty — using cached snapshot"
    cat "$MEMORY_CACHE_FILE"
  else
    log_err "Memory load failed and no cache available"
    printf '%s' "## Memory unavailable\n(memory loader failed — Claude is operating without context)"
  fi
}

# ── Send to Lark (only user-facing content — never errors) ────────────
# Thin wrapper around the Lark plugin's lark_send, with a local log line.
send_to_lark() {
  local content="$1"
  [ -z "$content" ] && return
  if ! lark_send "$content"; then
    log_warn "Failed to send message to Lark"
  fi
}

# ── Check if Claude output looks like an error (reuses core/safety.py) ─
looks_like_error() {
  JV_TEXT="$1" python3 -c "
import os, sys
sys.path.insert(0, '$JARVIS_DIR')
from core.safety import looks_like_error
sys.exit(0 if looks_like_error(os.environ.get('JV_TEXT','')) else 1)
" 2>/dev/null
}

# ── Heartbeat Loop (background) ──────────────────────────────────────
heartbeat_loop() {
  sleep 3
  log_info "[heartbeat] Starting (${CHECK_INTERVAL}s cycle)..."

  while true; do
    local force_flag=""
    if [ -f "$HEARTBEAT_TRIGGER" ]; then
      force_flag="--force"
      rm -f "$HEARTBEAT_TRIGGER"
      log_info "[heartbeat] Force trigger detected"
    fi

    # CRITICAL: stderr goes to log file, stdout is captured.
    # Only non-empty stdout is considered a user message for Lark.
    local output
    output=$(python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.heartbeat import HeartbeatRunner
runner = HeartbeatRunner(
    jarvis_dir='$JARVIS_DIR',
    heartbeat_file='$JARVIS_DIR/HEARTBEAT.md',
    state_file='$JARVIS_DIR/heartbeat_state.json',
    memory_dir='$MEMORY_DIR',
    model='$HEARTBEAT_MODEL',
    work_dir='$WORK_DIR',
)
result = runner.run_cycle($( [ -n "$force_flag" ] && echo "force=True" || echo ""))
if result:
    print(result)
" 2>>"$LOG_FILE") || {
      log_warn "[heartbeat] Cycle exited with non-zero status"
      output=""
    }

    if [ -n "$output" ] && ! looks_like_error "$output"; then
      send_to_lark "$output"
      log_info "[heartbeat] Beat sent"
    elif [ -n "$output" ]; then
      log_warn "[heartbeat] Suppressed error-like output (see log)"
    fi

    sleep "$CHECK_INTERVAL"
  done
}

heartbeat_loop &
HEARTBEAT_PID=$!
log_info "Heartbeat started (PID: $HEARTBEAT_PID)"

# ── Admin Console (optional, background) ─────────────────────────────
ADMIN_PID=""
if [ "$ADMIN_ENABLED" = "true" ]; then
  python3 "$JARVIS_DIR/admin.py" >>"$LOG_FILE" 2>&1 &
  ADMIN_PID=$!
  log_info "Admin started (PID: $ADMIN_PID) — http://$ADMIN_HOST:$ADMIN_PORT"
else
  log_info "Admin disabled (set admin.enabled: true in jarvis.yaml to enable)"
fi

# Cleanup on exit
cleanup() {
  log_info "Shutting down..."
  [ -n "$ADMIN_PID" ] && kill "$ADMIN_PID" 2>/dev/null || true
  kill "$HEARTBEAT_PID" 2>/dev/null || true
  wait "$HEARTBEAT_PID" 2>/dev/null || true
  [ -n "$ADMIN_PID" ] && wait "$ADMIN_PID" 2>/dev/null || true
  log_info "Stopped."
}
trap cleanup EXIT INT TERM

# ── Lark Message Listener (foreground) ────────────────────────────────
if [ -z "$USER_ID" ]; then
  log_info "No Lark user_id configured. Running heartbeat-only mode."
  echo "  Press Ctrl+C to stop." >&2
  wait "$HEARTBEAT_PID"
  exit 0
fi

lark_subscribe_messages \
  | while IFS= read -r line; do
      # Skip SDK error lines (they shouldn't appear on stdout but just in case)
      case "$line" in "[SDK Error]"*) continue ;; esac

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
        lark_reply_text "$message_id" "Heartbeat triggered" >/dev/null
        continue
      fi

      # Determine session (conv_key = sender for p2p, chat_id for groups)
      if [ "$chat_type" = "p2p" ]; then
        conv_key="$sender_id"
      else
        conv_key="$chat_id"
      fi
      session_result=$(get_session_id "$conv_key" 2>&1)
      session_id=$(echo "$session_result" | tail -1)
      rotated=$(echo "$session_result" | grep ROTATED || true)

      if [ -n "$rotated" ]; then
        log_info "Session rotated for $conv_key → $session_id"
        python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.heartbeat import HeartbeatRunner
runner = HeartbeatRunner('$JARVIS_DIR', '$JARVIS_DIR/HEARTBEAT.md',
    '$JARVIS_DIR/heartbeat_state.json', '$MEMORY_DIR', '$HEARTBEAT_MODEL',
    work_dir='$WORK_DIR')
runner.run_cycle(force=True, only_task='memory-hourly')
" 2>>"$LOG_FILE" >/dev/null || log_warn "Memory hourly on rotation failed"
      fi

      log_info "[$session_id] Received: $content"

      # Send "Thinking..." indicator (captures message_id for later deletion)
      thinking_result=$(lark_reply_text "$message_id" "Thinking...")
      thinking_id=$(echo "$thinking_result" | jq -r '.data.message_id // empty' 2>/dev/null || true)

      # Build system prompt with memory + recent turns
      memory=$(load_memory)
      now_ts=$(date '+%Y-%m-%d %H:%M %A')

      counter=$(JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" python3 -c "
import json, os
try:
    print(json.load(open(os.environ['JV_TRACKER'])).get(os.environ['JV_KEY'], {}).get('counter', 0))
except Exception:
    print(0)
" 2>>"$LOG_FILE" || echo 0)

      recent_turns=$(JV_SDIR="$CLAUDE_PROJECT_DIR" JV_SID="$session_id" \
        JV_COUNTER="$counter" JV_KEY="$conv_key" python3 -c "
import os, sys
sys.path.insert(0, '$JARVIS_DIR')
from core.session import build_recent_turns
print(build_recent_turns(os.environ['JV_SDIR'], os.environ['JV_SID'],
                         int(os.environ['JV_COUNTER']), os.environ['JV_KEY'], 20))
" 2>>"$LOG_FILE" || echo "")

      sys_prompt="You are a personal assistant and life mentor. Reply in the same language the user uses.
Current time: $now_ts

$memory

$recent_turns"

      # Call Claude (runs in WORK_DIR, with optional timeout)
      session_file="$CLAUDE_PROJECT_DIR/${session_id}.jsonl"
      if [ -f "$session_file" ]; then
        log_info "[$session_id] Resuming session"
        answer=$(cd "$WORK_DIR" && with_timeout 120 claude -p "$content" \
          --resume "$session_id" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          < /dev/null 2>>"$LOG_FILE" || true)
      else
        log_info "[$session_id] New session"
        answer=$(cd "$WORK_DIR" && with_timeout 120 claude -p "$content" \
          --session-id "$session_id" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          < /dev/null 2>>"$LOG_FILE" || true)
      fi

      # Filter error-like answers — never send them to the user as the "real" reply
      reply=""
      if [ -n "$answer" ] && ! looks_like_error "$answer"; then
        reply="$answer"
      fi

      if [ -z "$reply" ]; then
        log_warn "[$session_id] Empty/error answer from Claude (${#answer} chars)"
        [ -n "$thinking_id" ] && lark_delete_message "$thinking_id"
        lark_reply_text "$message_id" \
          "Sorry, I couldn't generate a response just now. Please try again in a moment." >/dev/null
        continue
      fi

      log_info "[$session_id] Replied (${#reply} chars)"

      # Delete "Thinking..." and send the real reply
      [ -n "$thinking_id" ] && lark_delete_message "$thinking_id"
      if ! lark_reply "$message_id" "$reply"; then
        log_err "[$session_id] Failed to send reply to Lark"
      fi
    done
