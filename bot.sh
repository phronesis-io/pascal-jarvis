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

# ── Single-instance lock (prevent duplicate replies) ────────────────
PIDFILE="$JARVIS_DIR/.bot.pid"
if [ -f "$PIDFILE" ]; then
  old_pid=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo "bot.sh is already running (PID $old_pid). Exiting." >&2
    exit 1
  fi
  # Stale pidfile — previous crash
  rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"

LOG_FILE="$JARVIS_DIR/jarvis.log"
LOG_MAX_BYTES=500000  # 500KB — rotate on startup if exceeded
MEMORY_CACHE_FILE="$JARVIS_DIR/.memory_cache"   # last-known-good memory snapshot

# ── Log rotation (on startup) ────────────────────────────────────────
if [ -f "$LOG_FILE" ] && [ "$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
  tail -500 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

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
emit("APP_ID", c.lark.get("app_id", ""))
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

JOBS_DIR="$JARVIS_DIR/jobs"
MAX_HANDLERS=5   # max concurrent message handlers

mkdir -p "$DATA_DIR" "$MEMORY_DIR" "$MEMORY_DIR/hot" "$MEMORY_DIR/warm" \
         "$MEMORY_DIR/timeline" "$MEMORY_DIR/system" "$JARVIS_DIR/eigenflux" \
         "$JOBS_DIR"
[ -f "$SESSION_TRACKER" ] || echo "{}" > "$SESSION_TRACKER"

# Clean up stale session locks from previous crashes/restarts
rm -f "$JARVIS_DIR"/.session_lock_* 2>/dev/null

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

  # Trim outbox to last 20 entries on startup (prevents unbounded growth)
  local _outbox="$JARVIS_DIR/heartbeat_outbox.jsonl"
  if [ -f "$_outbox" ] && [ "$(wc -l < "$_outbox")" -gt 20 ]; then
    tail -20 "$_outbox" > "$_outbox.tmp" && mv "$_outbox.tmp" "$_outbox"
  fi

  # Trim engagement log to last 500 entries (keeps ~2 weeks of data)
  local _elog="$JARVIS_DIR/engagement_log.jsonl"
  if [ -f "$_elog" ] && [ "$(wc -l < "$_elog")" -gt 500 ]; then
    tail -500 "$_elog" > "$_elog.tmp" && mv "$_elog.tmp" "$_elog"
  fi

  while true; do
    # Check for restart trigger in heartbeat loop too (message loop may be idle)
    if [ -f "$JARVIS_DIR/.restart_trigger" ]; then
      rm -f "$JARVIS_DIR/.restart_trigger"
      log_info "Restart triggered from heartbeat loop — exec-ing self..."
      # Kill parent and children, then restart
      kill 0 2>/dev/null || true
      exec "$JARVIS_DIR/bot.sh"
    fi

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
      if [[ "$output" == '{"config":'* ]]; then
        lark_send_card "$output"
      else
        send_to_lark "$output"
      fi
      # Write to outbox so main session can see what heartbeat sent
      local ts_iso
      ts_iso=$(date '+%Y-%m-%d %H:%M')
      printf '%s\n' "{\"role\":\"assistant\",\"text\":$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$output" 2>/dev/null),\"ts\":\"$ts_iso\",\"source\":\"heartbeat\"}" \
        >> "$JARVIS_DIR/heartbeat_outbox.jsonl"
      # Record heartbeat send for engagement tracking
      local _hb_source
      _hb_source=$(python3 -c "
import json, sys
try:
    # Detect source from heartbeat state (last task that produced output)
    with open('$JARVIS_DIR/heartbeat_state.json') as f:
        state = json.load(f)
    # Find most recently run task
    latest = max(state.items(), key=lambda x: x[1].get('last_run', 0))
    print(latest[0])
except Exception:
    print('heartbeat')
" 2>/dev/null || echo "heartbeat")
      printf '%s\n' "{\"ts\":\"$ts_iso\",\"source\":\"$_hb_source\",\"type\":\"sent\",\"epoch\":$(date +%s)}" \
        >> "$JARVIS_DIR/engagement_log.jsonl"
      log_info "[heartbeat] Beat sent"
    elif [ -n "$output" ]; then
      log_warn "[heartbeat] Suppressed error-like output (see log)"
    else
      log_info "[heartbeat] Beat sent (idle)"
    fi

    sleep "$CHECK_INTERVAL"
  done
}

heartbeat_loop &
HEARTBEAT_PID=$!

# ── EigenFlux Real-Time Stream (background) ─────────────────────────
# Spawns `eigenflux stream` for real-time private message delivery.
# Reconnects with exponential backoff on failure.
eigenflux_stream_loop() {
  export PATH="$HOME/.local/bin:$PATH"
  command -v eigenflux >/dev/null 2>&1 || {
    log_info "[ef-stream] eigenflux CLI not installed, skipping stream"
    return 0
  }

  sleep 5  # let heartbeat settle first
  log_info "[ef-stream] Starting real-time message stream..."

  local backoff=1 max_backoff=60 failures=0 max_failures=20
  local cursor_file="/tmp/jarvis-ef-cursor"

  while true; do
    local args="stream -f json"
    local cursor=""
    [ -f "$cursor_file" ] && cursor=$(cat "$cursor_file" 2>/dev/null)
    [ -n "$cursor" ] && args="$args --cursor $cursor"

    log_info "[ef-stream] Spawning: eigenflux $args"

    # Read NDJSON lines from eigenflux stream
    eigenflux $args 2>>"$LOG_FILE" | while IFS= read -r line; do
      [ -z "$line" ] && continue

      # Extract cursor for reconnect resume (persist to file — pipe subshell can't update parent vars)
      new_cursor=$(echo "$line" | python3 -c "
import sys, json
try:
    e = json.loads(sys.stdin.read())
    c = e.get('data', {}).get('next_cursor', '')
    if c: print(c)
except: pass
" 2>/dev/null || true)
      [ -n "$new_cursor" ] && printf '%s' "$new_cursor" > "$cursor_file"

      # Format message for Lark delivery (enriched with conv_id for reply)
      msg=$(JV_LINE="$line" python3 -c "
import os, sys, json
try:
    event = json.loads(os.environ['JV_LINE'])
    messages = event.get('data', {}).get('messages', [])
    if not messages:
        sys.exit(0)
    parts = []
    for m in messages:
        sender = m.get('sender_name', 'Unknown agent')
        content = m.get('content', '')
        conv_id = m.get('conv_id', '')
        if content:
            parts.append(f'💬 **{sender}**: {content}')
    if parts:
        print('\n'.join(parts) + '\n\n📡 Powered by EigenFlux')
except Exception:
    sys.exit(0)
" 2>/dev/null || true)

      if [ -n "$msg" ]; then
        send_to_lark "$msg"
        # Write to outbox with conv metadata so main session can reply directly
        local ts_iso _outbox_meta
        ts_iso=$(date '+%Y-%m-%d %H:%M')
        _outbox_meta=$(JV_LINE="$line" python3 -c "
import os, json, sys
try:
    event = json.loads(os.environ['JV_LINE'])
    msgs = event.get('data', {}).get('messages', [])
    if msgs:
        m = msgs[0]
        meta = {'conv_id': m.get('conv_id',''), 'sender_id': m.get('sender_id',''), 'sender_name': m.get('sender_name','')}
        print(json.dumps(meta, ensure_ascii=False))
except: pass
" 2>/dev/null || true)
        printf '%s\n' "{\"role\":\"assistant\",\"text\":$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$msg" 2>/dev/null),\"ts\":\"$ts_iso\",\"source\":\"eigenflux-stream\",\"meta\":${_outbox_meta:-\"{}\"}}" \
          >> "$JARVIS_DIR/heartbeat_outbox.jsonl"
        log_info "[ef-stream] Delivered real-time message"

        # Analyze message through Claude in background, send follow-up if useful
        local _stream_detail
        _stream_detail=$(JV_LINE="$line" python3 -c "
import os, json, sys
try:
    event = json.loads(os.environ['JV_LINE'])
    messages = event.get('data', {}).get('messages', [])
    for m in messages:
        sender = m.get('sender_name', 'Unknown')
        sender_id = m.get('sender_id', '')
        content = m.get('content', '')
        item_id = m.get('item_id', '')
        conv_id = m.get('conv_id', '')
        print(json.dumps({'sender': sender, 'sender_id': sender_id,
                          'content': content, 'item_id': item_id,
                          'conv_id': conv_id}, ensure_ascii=False))
except: pass
" 2>/dev/null || true)

        if [ -n "$_stream_detail" ]; then
          (
            # Load friend list for context
            local _friends_ctx=""
            local _contacts_file
            _contacts_file=$(find "$HOME/.eigenflux" -name contacts.json 2>/dev/null | head -1)
            if [ -f "$_contacts_file" ]; then
              _friends_ctx=$(python3 -c "
import json, sys
try:
    friends = json.load(open('$_contacts_file'))
    if isinstance(friends, list):
        names = [f.get('agent_name','') + ' (remark: ' + f.get('remark','') + ')' for f in friends[:20] if f.get('agent_name')]
        print('Known friends: ' + ', '.join(names))
except: pass
" 2>/dev/null || true)
            fi

            # Background: run a quick Claude analysis of the incoming message
            local _analysis
            _analysis=$(claude \
              --model sonnet \
              --dangerously-skip-permissions \
              --no-session-persistence \
              --disable-slash-commands \
              -p "[EIGENFLUX REAL-TIME MESSAGE — Quick Analysis]
A private message just arrived on EigenFlux:
$_stream_detail

${_friends_ctx}

The raw message was already forwarded to the user. Your job:
1. Check memory: is this sender someone we know? What's our relationship?
2. If the message requires a response, suggest what to reply (brief, concrete)
3. If there's context the user needs (e.g. this relates to a current project), provide it

If the message is routine/no action needed, reply HEARTBEAT_OK.
Otherwise reply with a brief Chinese note (≤60 words) for the user." \
              < /dev/null 2>>"$LOG_FILE" || true)

            if [ -n "$_analysis" ] && ! echo "$_analysis" | grep -q "HEARTBEAT_OK"; then
              send_to_lark "💡 $_analysis"
              log_info "[ef-stream] Follow-up analysis sent"
            fi
          ) &
        fi

        backoff=1
        failures=0
      fi
    done

    # Stream process exited
    local exit_code=${PIPESTATUS[0]:-0}

    if [ "$exit_code" -eq 4 ]; then
      log_warn "[ef-stream] Auth required — token may be expired"
      send_to_lark "⚠️ EigenFlux token expired. Please re-authenticate: \`eigenflux auth login\`"
      # Wait longer before retry on auth issues
      sleep 300
      continue
    fi

    failures=$((failures + 1))
    if [ "$failures" -ge "$max_failures" ]; then
      log_err "[ef-stream] Giving up after $max_failures consecutive failures"
      return 1
    fi

    log_info "[ef-stream] Reconnecting in ${backoff}s (failure #$failures)"
    sleep "$backoff"
    backoff=$((backoff * 2))
    [ "$backoff" -gt "$max_backoff" ] && backoff="$max_backoff"
  done
}

eigenflux_stream_loop &
STREAM_PID=$!
log_info "Heartbeat started (PID: $HEARTBEAT_PID)"
log_info "EigenFlux stream started (PID: $STREAM_PID)"

# ── Admin Console (optional, background) ─────────────────────────────
ADMIN_PID=""
if [ "$ADMIN_ENABLED" = "true" ]; then
  python3 "$JARVIS_DIR/admin.py" >>"$LOG_FILE" 2>&1 &
  ADMIN_PID=$!
  log_info "Admin started (PID: $ADMIN_PID) — http://$ADMIN_HOST:$ADMIN_PORT"
else
  log_info "Admin disabled (set admin.enabled: true in jarvis.yaml to enable)"
fi

# ── Action Post-Processor ────────────────────────────────────────────
# Scans Claude's reply for [ACTION:...] markers, executes them, and
# returns the cleaned reply with any action results appended.
# Usage: cleaned_reply=$(process_actions "$reply" "$conv_key" "$message_id")
process_actions() {
  local reply="$1" conv_key="$2" message_id="$3"
  local action_results=""

  # Extract all action markers
  local actions
  actions=$(echo "$reply" | grep -o '\[ACTION:[^]]*\]' 2>/dev/null || true)

  if [ -z "$actions" ]; then
    printf '%s' "$reply"
    return
  fi

  while IFS= read -r marker; do
    [ -z "$marker" ] && continue
    # Parse action type and params
    local action_body="${marker#\[ACTION:}"
    action_body="${action_body%\]}"
    local action_type="${action_body%%|*}"
    local action_params="${action_body#*|}"
    [ "$action_params" = "$action_type" ] && action_params=""

    case "$action_type" in
      feed_search)
        local query="${action_params#query=}"
        local results
        results=$(JV_QUERY="$query" python3 -c "
import sys, os
sys.path.insert(0, '$JARVIS_DIR')
from plugins.eigenflux.client import EigenFluxClient
c = EigenFluxClient('eigenflux')
items = c.search_feed_history(os.environ['JV_QUERY'], limit=5)
if not items:
    print('没找到相关内容')
else:
    for item in items:
        title = item.get('title', item.get('content', '')[:60])
        url = item.get('url', '')
        ts = item.get('fetched_at', '')[:16]
        print(f'• [{ts}] {title}')
        if url:
            print(f'  {url}')
        print()
" 2>/dev/null || echo "搜索失败")
        if [ -n "$results" ]; then
          action_results="${action_results}
${results}"
        fi
        ;;

      watchlater)
        # Parse title and url from params: title=<title>|url=<url>
        local wl_title="" wl_url=""
        wl_title=$(echo "$action_params" | sed -n 's/.*title=\([^|]*\).*/\1/p')
        wl_url=$(echo "$action_params" | sed -n 's/.*url=\([^|]*\).*/\1/p')
        if [ -n "$wl_url" ]; then
          python3 "$JARVIS_DIR/tasks/watchlater_save.py" "$wl_title" "$wl_url" "action" \
            2>>"$LOG_FILE" >/dev/null || true
          log_info "[action] watchlater saved: $wl_title"
        fi
        ;;

      bg)
        local bg_prompt="${action_params#prompt=}"
        local bg_desc="${bg_prompt:0:80}"
        local job_id
        job_id=$(JV_JOBS_DIR="$JOBS_DIR" JV_CONV_KEY="$conv_key" \
          JV_DESC="$bg_desc" JV_MSG_ID="$message_id" \
          python3 "$JARVIS_DIR/core/jobs.py" create 2>>"$LOG_FILE" || echo "")
        if [ -n "$job_id" ]; then
          log_info "[bg:$job_id] Started via action: $bg_desc"
          run_background_job "$job_id" "$conv_key" "$bg_prompt" "$message_id" &
          action_results="${action_results}
⏳ Job ID: \`$job_id\`"
        fi
        ;;

      jobs)
        local jobs_output
        jobs_output=$(JV_JOBS_DIR="$JOBS_DIR" JV_CONV_KEY="$conv_key" \
          python3 "$JARVIS_DIR/core/jobs.py" list 2>>"$LOG_FILE" || echo "Failed to list jobs")
        action_results="${action_results}
${jobs_output}"
        ;;

      job_cancel)
        local cancel_id="${action_params#id=}"
        local cancel_result
        cancel_result=$(JV_JOBS_DIR="$JOBS_DIR" \
          python3 "$JARVIS_DIR/core/jobs.py" cancel "$cancel_id" 2>>"$LOG_FILE" || echo "error")
        if [ "$cancel_result" = "cancelled" ]; then
          action_results="${action_results}
Job $cancel_id cancelled."
        else
          action_results="${action_results}
Job not found or not running: $cancel_id"
        fi
        ;;

      job_output)
        local out_id="${action_params#id=}"
        local out_file="$JOBS_DIR/${out_id}/output.md"
        if [ -f "$out_file" ]; then
          local out_content
          out_content=$(cat "$out_file" 2>/dev/null)
          if [ ${#out_content} -gt 4000 ]; then
            out_content="${out_content:0:4000}

... (truncated)"
          fi
          action_results="${action_results}
${out_content}"
        else
          action_results="${action_results}
No output found for job: $out_id"
        fi
        ;;

      heartbeat)
        touch "$HEARTBEAT_TRIGGER"
        log_info "[action] Heartbeat triggered"
        ;;

      calendar_create)
        # Parse params: title=<title>|start=<ISO>|end=<ISO>|desc=<desc>
        local cal_title="" cal_start="" cal_end="" cal_desc=""
        cal_title=$(echo "$action_params" | sed -n 's/.*title=\([^|]*\).*/\1/p')
        cal_start=$(echo "$action_params" | sed -n 's/.*start=\([^|]*\).*/\1/p')
        cal_end=$(echo "$action_params" | sed -n 's/.*end=\([^|]*\).*/\1/p')
        cal_desc=$(echo "$action_params" | sed -n 's/.*desc=\([^|]*\).*/\1/p')
        if [ -n "$cal_title" ] && [ -n "$cal_start" ] && [ -n "$cal_end" ]; then
          # Conflict detection: check if slot is free
          local is_busy
          is_busy=$(lark-cli calendar +freebusy --as user --start "$cal_start" --end "$cal_end" 2>/dev/null \
            | python3 -c "import json,sys; d=json.load(sys.stdin); print('busy' if d.get('busy') else 'free')" 2>/dev/null || echo "free")
          if [ "$is_busy" = "busy" ]; then
            action_results="${action_results}
⚠️ 时间段有冲突，但仍创建了: $cal_title ($cal_start → $cal_end)"
            log_warn "[action] calendar_create conflict detected: $cal_title at $cal_start"
          fi
          local cal_result
          if [ -n "$cal_desc" ]; then
            cal_result=$(lark-cli calendar +create --as user --summary "$cal_title" --start "$cal_start" --end "$cal_end" --description "$cal_desc" 2>>"$LOG_FILE" || echo "FAILED")
          else
            cal_result=$(lark-cli calendar +create --as user --summary "$cal_title" --start "$cal_start" --end "$cal_end" 2>>"$LOG_FILE" || echo "FAILED")
          fi
          if echo "$cal_result" | grep -q "FAILED"; then
            action_results="${action_results}
❌ 日程创建失败: $cal_title"
          else
            if [ "$is_busy" != "busy" ]; then
              action_results="${action_results}
✅ 已创建日程: $cal_title ($cal_start → $cal_end)"
            fi
          fi
          log_info "[action] calendar_create: $cal_title"
        fi
        ;;

      calendar_update)
        # Parse params: event_id=<id>|field=<summary|start|end>|value=<new>
        local cal_event_id="" cal_field="" cal_value=""
        cal_event_id=$(echo "$action_params" | sed -n 's/.*event_id=\([^|]*\).*/\1/p')
        cal_field=$(echo "$action_params" | sed -n 's/.*field=\([^|]*\).*/\1/p')
        cal_value=$(echo "$action_params" | sed -n 's/.*value=\([^|]*\).*/\1/p')
        if [ -n "$cal_event_id" ] && [ -n "$cal_field" ] && [ -n "$cal_value" ]; then
          local update_data=""
          update_data=$(JV_FIELD="$cal_field" JV_VALUE="$cal_value" python3 -c "
import os, json
field = os.environ['JV_FIELD']
value = os.environ['JV_VALUE']
if field == 'summary':
    print(json.dumps({'summary': value}))
elif field == 'start':
    print(json.dumps({'start_time': {'timestamp': value}}))
elif field == 'end':
    print(json.dumps({'end_time': {'timestamp': value}}))
else:
    print(json.dumps({'description': value}))
" 2>/dev/null || echo "{}")
          local update_result
          update_result=$(lark-cli calendar events patch --as user \
            --params "{\"event_id\":\"$cal_event_id\"}" \
            --data "$update_data" 2>>"$LOG_FILE" || echo "FAILED")
          if echo "$update_result" | grep -q "FAILED"; then
            action_results="${action_results}
❌ 日程更新失败: $cal_event_id ($cal_field)"
          else
            action_results="${action_results}
✅ 已更新日程: $cal_field → $cal_value"
          fi
          log_info "[action] calendar_update: $cal_event_id $cal_field=$cal_value"
        fi
        ;;

      task_create)
        # Parse params: title=<title>|due=<ISO_optional>
        local task_title="" task_due=""
        task_title=$(echo "$action_params" | sed -n 's/.*title=\([^|]*\).*/\1/p')
        task_due=$(echo "$action_params" | sed -n 's/.*due=\([^|]*\).*/\1/p')
        if [ -n "$task_title" ]; then
          local task_result
          if [ -n "$task_due" ]; then
            task_result=$(lark-cli task +create --as user --summary "$task_title" --due "$task_due" 2>>"$LOG_FILE" || echo "FAILED")
          else
            task_result=$(lark-cli task +create --as user --summary "$task_title" 2>>"$LOG_FILE" || echo "FAILED")
          fi
          if echo "$task_result" | grep -q "FAILED"; then
            action_results="${action_results}
❌ 任务创建失败: $task_title"
          else
            action_results="${action_results}
✅ 已创建任务: $task_title"
          fi
          log_info "[action] task_create: $task_title"
        fi
        ;;

      task_complete)
        # Parse params: task_id=<id>
        local task_id=""
        task_id=$(echo "$action_params" | sed -n 's/.*task_id=\([^|]*\).*/\1/p')
        if [ -n "$task_id" ]; then
          local complete_result
          complete_result=$(lark-cli task +complete --as user --task-id "$task_id" 2>>"$LOG_FILE" || echo "FAILED")
          if echo "$complete_result" | grep -q "FAILED"; then
            action_results="${action_results}
❌ 任务完成标���失败: $task_id"
          else
            action_results="${action_results}
✅ 任务已完成"
          fi
          log_info "[action] task_complete: $task_id"
        fi
        ;;

      # ── Local Task System (tasks.jsonl) ──────────────────────────────
      task_capture)
        # Parse params: title=<title>|type=<praxis|poiesis>|energy=<h|m|l>|est=<min>|due=<date>
        local tc_title="" tc_type="poiesis" tc_energy="medium" tc_est="30" tc_due=""
        tc_title=$(echo "$action_params" | sed -n 's/.*title=\([^|]*\).*/\1/p')
        tc_type=$(echo "$action_params" | sed -n 's/.*type=\([^|]*\).*/\1/p')
        tc_energy=$(echo "$action_params" | sed -n 's/.*energy=\([^|]*\).*/\1/p')
        tc_est=$(echo "$action_params" | sed -n 's/.*est=\([^|]*\).*/\1/p')
        tc_due=$(echo "$action_params" | sed -n 's/.*due=\([^|]*\).*/\1/p')
        [ -z "$tc_type" ] && tc_type="poiesis"
        [ -z "$tc_energy" ] && tc_energy="medium"
        [ -z "$tc_est" ] && tc_est="30"
        if [ -n "$tc_title" ]; then
          local tc_result
          tc_result=$(JV_TITLE="$tc_title" JV_TYPE="$tc_type" JV_ENERGY="$tc_energy" \
            JV_EST="$tc_est" JV_DUE="$tc_due" python3 -c "
import os, sys, json
sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path
tm = TaskManager(Path('$MEMORY_DIR'))
t = tm.capture(
    title=os.environ['JV_TITLE'],
    type=os.environ.get('JV_TYPE', 'poiesis'),
    energy=os.environ.get('JV_ENERGY', 'medium'),
    time_est_min=int(os.environ.get('JV_EST', '30')),
    due=os.environ.get('JV_DUE') or None,
    source='conversation',
)
print(t['id'])
" 2>>"$LOG_FILE" || echo "FAILED")
          if [ "$tc_result" != "FAILED" ] && [ -n "$tc_result" ]; then
            log_info "[action] task_capture: $tc_title ($tc_result)"
          fi
        fi
        ;;

      task_commit)
        # Parse params: id=<task_id>|when=<ISO8601_or_today>
        local tc_id="" tc_when=""
        tc_id=$(echo "$action_params" | sed -n 's/.*id=\([^|]*\).*/\1/p')
        tc_when=$(echo "$action_params" | sed -n 's/.*when=\([^|]*\).*/\1/p')
        if [ -n "$tc_id" ]; then
          python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path
tm = TaskManager(Path('$MEMORY_DIR'))
tm.commit('$tc_id', when='$tc_when' or None)
" 2>>"$LOG_FILE" || true
          log_info "[action] task_commit: $tc_id when=$tc_when"
        fi
        ;;

      task_done)
        # Parse params: id=<task_id>
        local td_id=""
        td_id=$(echo "$action_params" | sed -n 's/.*id=\([^|]*\).*/\1/p')
        if [ -n "$td_id" ]; then
          python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path
tm = TaskManager(Path('$MEMORY_DIR'))
tm.done('$td_id')
" 2>>"$LOG_FILE" || true
          log_info "[action] task_done: $td_id"
        fi
        ;;

      task_reject)
        # Parse params: id=<task_id>|reason=<brief>
        local tr_id="" tr_reason=""
        tr_id=$(echo "$action_params" | sed -n 's/.*id=\([^|]*\).*/\1/p')
        tr_reason=$(echo "$action_params" | sed -n 's/.*reason=\([^|]*\).*/\1/p')
        if [ -n "$tr_id" ]; then
          JV_REASON="$tr_reason" python3 -c "
import os, sys; sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path
tm = TaskManager(Path('$MEMORY_DIR'))
tm.reject('$tr_id', os.environ.get('JV_REASON', ''))
" 2>>"$LOG_FILE" || true
          log_info "[action] task_reject: $tr_id"
        fi
        ;;

      task_defer)
        # Parse params: id=<task_id>|to=<date>
        local tdf_id="" tdf_to=""
        tdf_id=$(echo "$action_params" | sed -n 's/.*id=\([^|]*\).*/\1/p')
        tdf_to=$(echo "$action_params" | sed -n 's/.*to=\([^|]*\).*/\1/p')
        if [ -n "$tdf_id" ] && [ -n "$tdf_to" ]; then
          python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path
tm = TaskManager(Path('$MEMORY_DIR'))
tm.defer('$tdf_id', '$tdf_to')
" 2>>"$LOG_FILE" || true
          log_info "[action] task_defer: $tdf_id to=$tdf_to"
        fi
        ;;

      praxis_done)
        # Parse params: id=<praxis_id>
        local pd_id=""
        pd_id=$(echo "$action_params" | sed -n 's/.*id=\([^|]*\).*/\1/p')
        if [ -n "$pd_id" ]; then
          python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path
tm = TaskManager(Path('$MEMORY_DIR'))
tm.praxis_done('$pd_id')
" 2>>"$LOG_FILE" || true
          log_info "[action] praxis_done: $pd_id"
        fi
        ;;

      praxis_add)
        # Parse params: title=<title>|freq=<daily|weekly>|time=<HH:MM>|dur=<min>
        local pa_title="" pa_freq="daily" pa_time="08:30" pa_dur="20"
        pa_title=$(echo "$action_params" | sed -n 's/.*title=\([^|]*\).*/\1/p')
        pa_freq=$(echo "$action_params" | sed -n 's/.*freq=\([^|]*\).*/\1/p')
        pa_time=$(echo "$action_params" | sed -n 's/.*time=\([^|]*\).*/\1/p')
        pa_dur=$(echo "$action_params" | sed -n 's/.*dur=\([^|]*\).*/\1/p')
        [ -z "$pa_freq" ] && pa_freq="daily"
        [ -z "$pa_time" ] && pa_time="08:30"
        [ -z "$pa_dur" ] && pa_dur="20"
        if [ -n "$pa_title" ]; then
          JV_TITLE="$pa_title" JV_FREQ="$pa_freq" JV_TIME="$pa_time" JV_DUR="$pa_dur" \
            python3 -c "
import os, sys; sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path
tm = TaskManager(Path('$MEMORY_DIR'))
tm.praxis_add(
    title=os.environ['JV_TITLE'],
    frequency=os.environ.get('JV_FREQ', 'daily'),
    preferred_time=os.environ.get('JV_TIME', '08:30'),
    duration_min=int(os.environ.get('JV_DUR', '20')),
)
" 2>>"$LOG_FILE" || true
          log_info "[action] praxis_add: $pa_title"
        fi
        ;;

      praxis_remove)
        # Parse params: id=<praxis_id>
        local pr_id=""
        pr_id=$(echo "$action_params" | sed -n 's/.*id=\([^|]*\).*/\1/p')
        if [ -n "$pr_id" ]; then
          python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.tasks import TaskManager
from pathlib import Path
tm = TaskManager(Path('$MEMORY_DIR'))
tm.praxis_remove('$pr_id')
" 2>>"$LOG_FILE" || true
          log_info "[action] praxis_remove: $pr_id"
        fi
        ;;

      calendar_delete)
        # Parse params: event_id=<id>|title=<title>
        local cal_event_id="" cal_del_title=""
        cal_event_id=$(echo "$action_params" | sed -n 's/.*event_id=\([^|]*\).*/\1/p')
        cal_del_title=$(echo "$action_params" | sed -n 's/.*title=\([^|]*\).*/\1/p')
        if [ -n "$cal_event_id" ]; then
          local del_result
          del_result=$(lark-cli calendar events delete --as user --event-id "$cal_event_id" 2>>"$LOG_FILE" || echo "FAILED")
          if echo "$del_result" | grep -q "FAILED"; then
            action_results="${action_results}
❌ 日程删除失败: ${cal_del_title:-$cal_event_id}"
          else
            action_results="${action_results}
✅ 已删除日程: ${cal_del_title:-$cal_event_id}"
          fi
          log_info "[action] calendar_delete: $cal_event_id"
        fi
        ;;
    esac
  done <<< "$actions"

  # Strip all [ACTION:...] markers and collapse blank lines (macOS-safe)
  local cleaned
  cleaned=$(echo "$reply" | sed 's/\[ACTION:[^]]*\]//g' | grep -v '^[[:space:]]*$' || true)

  # Append action results if any
  if [ -n "$action_results" ]; then
    printf '%s\n%s' "$cleaned" "$action_results"
  else
    printf '%s' "$cleaned"
  fi
}

# ── Message Handler (runs in background subshell) ────────────────────
# Extracted from the main loop so different conversations run in parallel.
# Same-session messages serialize via the existing lock file mechanism.
handle_message() {
  local conv_key="$1" content="$2" message_id="$3" session_id="$4"
  local reaction_id="$5"

  # Build system prompt with memory + recent turns
  local memory now_ts counter recent_turns ef_skills sys_prompt
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

  # Load EigenFlux skill docs (strip YAML frontmatter)
  ef_skills=""
  for _skill_dir in "$JARVIS_DIR/plugins/eigenflux/skills"/ef-*/; do
    _skill_file="$_skill_dir/SKILL.md"
    [ -f "$_skill_file" ] || continue
    _skill_body=$(sed -n '/^---$/,/^---$/!p' "$_skill_file" 2>/dev/null)
    ef_skills="$ef_skills
$_skill_body
"
  done

  sys_prompt="You are a personal assistant and life mentor. Reply in the same language the user uses.
Current time: $now_ts

IMPORTANT: Never use EnterPlanMode or plan mode. You are running in a non-interactive messaging environment where plan mode confirmations will break the conversation flow. Just execute tasks directly.

## Available Actions

When the user's intent requires a system action, include the appropriate marker in your response.
The system will execute it and the result will be available. Actions:

- [ACTION:feed_search|query=<keyword>] — Search EigenFlux feed history. Use when user wants to find past articles/content.
- [ACTION:watchlater|title=<title>|url=<url>] — Save content for later. Use when user wants to bookmark/save something.
- [ACTION:bg|prompt=<task>] — Run a long task in background. Use when user requests research or complex work that will take a long time.
- [ACTION:jobs] — List active background jobs. Use when user asks about running tasks.
- [ACTION:job_cancel|id=<id>] — Cancel a background job.
- [ACTION:job_output|id=<id>] — Get output of a background job.
- [ACTION:heartbeat] — Trigger an immediate heartbeat cycle (system check).
- [ACTION:calendar_create|title=<title>|start=<ISO8601>|end=<ISO8601>|desc=<optional>] — Create a calendar event. Use when user confirms a schedule change or asks to add an event. System auto-checks conflicts.
- [ACTION:calendar_update|event_id=<id>|field=<summary|start|end>|value=<new_value>] — Update a calendar event (reschedule, rename). Use event_id from calendar_event_mapping.json.
- [ACTION:calendar_delete|event_id=<id>|title=<name>] — Delete a calendar event. Use when user confirms removing an event.
- [ACTION:task_create|title=<title>|due=<ISO8601_optional>] — Create a Lark Task (todo item).
- [ACTION:task_complete|task_id=<id>] — Mark a Lark Task as done.
- [ACTION:task_capture|title=<title>|type=<praxis|poiesis>|energy=<h|m|l>|est=<min>|due=<date>] — Capture a task into local inbox. type: praxis=becoming (exercise,reading,meditation), poiesis=producing (code,writing,deliverables). energy: h/m/l.
- [ACTION:task_commit|id=<task_id>|when=<ISO8601_or_today>] — Commit an inbox task to today/specific time.
- [ACTION:task_done|id=<task_id>] — Mark a local task as done.
- [ACTION:task_reject|id=<task_id>|reason=<brief>] — Explicitly reject a task (this is freedom, not failure).
- [ACTION:task_defer|id=<task_id>|to=<YYYY-MM-DD>] — Defer a task to another date.
- [ACTION:praxis_done|id=<praxis_id>] — Record that a praxis (habit/practice) was done today.
- [ACTION:praxis_add|title=<title>|freq=<daily|weekly>|time=<HH:MM>|dur=<min>] — Add a recurring praxis.
- [ACTION:praxis_remove|id=<praxis_id>] — Remove a praxis from the registry.

Rules:
- Include the action marker naturally in your response (it will be stripped before delivery)
- You can include multiple actions in one response
- For feed_search: the system will execute the search and append results to your response
- For watchlater: just confirm to the user that it's saved
- For bg: tell the user the task is running in the background
- For jobs/job_cancel/job_output: the system will execute and append results
- Always respond naturally in Chinese — the marker is just a signal to the system
- When a task will take a very long time (experiments, large research, batch processing),
  proactively suggest running it in the background using the bg action.
- For calendar actions: ALWAYS confirm with the user before creating/deleting events.
  Present the change clearly, get explicit confirmation (确认/好/可以), then include the action marker.
  Start/end times must be ISO8601 format (e.g. 2026-05-14T09:00:00+08:00).
  To find event_id for deletion/update, use \`lark-cli calendar +agenda --as user --format json\` first.
  Conflicts are auto-detected on create — system will warn but still create.
- For task actions (task_capture): capture when user mentions something they want/need to do.
  Use task_capture for the LOCAL task system (tracks decay, capacity, praxis).
  Use task_create for LARK tasks (lightweight, external).
  Prefer task_capture for things that need time-binding and follow-through tracking.
  Don't require explicit confirmation — capture is low-friction.
- For task_reject: celebrate rejection. It's an act of choosing what NOT to be.
  Never guilt-trip about rejected or decayed tasks.
- For praxis: these are practices (修行), not tasks to check off.
  Record praxis_done when user mentions completing a habit/practice.
- Implementation Intentions: when creating calendar events, gently ask WHY and suggest a trigger.
  Store in description: [WHY] reason [TRIGGER] when X happens [FIRST_ACTION] do Y first.
  This doubles follow-through (Gollwitzer, 1999). Don't force it — only when natural.

## Task System Philosophy
Tasks are commitments to finite time, not obligations to productivity.
- Praxis (修行/becoming) is protected before poiesis (造物/producing).
- Stale tasks are signals about authentic desire, not willpower failures.
- Decay is mercy — letting go of what no longer serves you.
- Capacity: max 5h/day of committed poiesis. Always leave whitespace.
- Rejection is freedom — choosing what NOT to do is an act of self-knowledge.

## EigenFlux Agent Network

You have the \`eigenflux\` CLI installed. Skills available:
$ef_skills
For detailed reference docs, read files in:
  $JARVIS_DIR/plugins/eigenflux/skills/ef-broadcast/references/
  $JARVIS_DIR/plugins/eigenflux/skills/ef-communication/references/
  $JARVIS_DIR/plugins/eigenflux/skills/ef-profile/references/

IMPORTANT: When presenting EigenFlux feed content to the user:
  - Always fetch the source URL via \`eigenflux feed get --item-id <ID>\`
  - Append '📡 Powered by EigenFlux' at the end
  - Never expose internal metadata (item_id, group_id, impression_id)

## Calendar Data

CRITICAL: For ANY schedule or time-related statements, ONLY use the data from the
calendar_today.md file in your memory. NEVER rely on schedule mentions from conversation
history (recent turns) — those may be stale/outdated from previous days.
If calendar_today.md says the next event is X, that is the truth.
If a previous conversation turn mentioned event Y, but calendar_today.md doesn't list it,
then Y is in the past — do NOT reference it as upcoming.

Calendar WRITE-BACK: You can create and delete events using the calendar_create and
calendar_delete actions. When you detect a conflict or the user asks for a schedule change:
1. Describe the proposed change clearly
2. Wait for user confirmation
3. Include the ACTION marker in your confirmed response
The system will execute it and the calendar will be updated immediately.

## Watch Later (收藏)

If the user mentions wanting to save, bookmark, or watch something later (e.g. '这个先收藏',
'以后看', '记一下这个链接', 'save this for later'), and the conversation contains or references
a specific URL + title, append this marker at the very end of your response (on its own line):
[SAVE_LATER: <title> | <url>]
This will be automatically parsed and saved. Do NOT mention this marker to the user.

$memory

$recent_turns"

  # Call Claude (runs in WORK_DIR, with optional timeout)
  # Wait for any existing session lock (previous message still processing)
  local LOCK_FILE="$JARVIS_DIR/.session_lock_${session_id}"
  local ANSWER_FILE
  ANSWER_FILE=$(mktemp /tmp/jarvis-answer-XXXXXX)
  local waited=0
  while [ -f "$LOCK_FILE" ] && [ "$waited" -lt 130 ]; do
    log_info "[$session_id] Session busy, waiting..."
    sleep 5
    waited=$((waited + 5))
  done

  # Run Claude in background so we can record its PID for "stop" command
  local session_file="$CLAUDE_PROJECT_DIR/${session_id}.jsonl"
  local _claude_pid
  if [ -f "$session_file" ]; then
    log_info "[$session_id] Resuming session"
    (cd "$WORK_DIR" && printf '%s' "$content" | with_timeout 600 claude -p \
      --resume "$session_id" \
      --append-system-prompt "$sys_prompt" \
      --dangerously-skip-permissions \
      2>>"$LOG_FILE" > "$ANSWER_FILE" || true) &
  else
    log_info "[$session_id] New session"
    (cd "$WORK_DIR" && printf '%s' "$content" | with_timeout 600 claude -p \
      --session-id "$session_id" \
      --append-system-prompt "$sys_prompt" \
      --dangerously-skip-permissions \
      2>>"$LOG_FILE" > "$ANSWER_FILE" || true) &
  fi
  _claude_pid=$!
  echo "$_claude_pid" > "$LOCK_FILE"
  wait $_claude_pid 2>/dev/null || true
  local answer
  answer=$(cat "$ANSWER_FILE" 2>/dev/null)
  rm -f "$ANSWER_FILE" "$LOCK_FILE"

  # Filter error-like answers — never send them to the user as the "real" reply
  local reply=""
  if [ -n "$answer" ] && ! looks_like_error "$answer"; then
    reply="$answer"
  fi

  if [ -z "$reply" ]; then
    log_warn "[$session_id] Empty/error answer from Claude (${#answer} chars)"
    [ -n "$reaction_id" ] && lark_remove_reaction "$message_id" "$reaction_id"
    lark_reply_text "$message_id" \
      "Sorry, I couldn't generate a response just now. Please try again in a moment." >/dev/null
    return
  fi

  # ── Process [ACTION:...] markers (LLM-driven action system) ──
  reply=$(process_actions "$reply" "$conv_key" "$message_id")

  # ── Detect [SAVE_LATER: title | url] markers and save to watchlater ──
  if echo "$reply" | grep -q '\[SAVE_LATER:'; then
    _sl_extracted=$(echo "$reply" | python3 -c "
import sys, re
text = sys.stdin.read()
matches = re.findall(r'\[SAVE_LATER:\s*(.+?)\s*\|\s*(https?://[^\]\s]+)\s*\]', text)
for title, url in matches:
    print(f'{title}\t{url}')
" 2>/dev/null)
    while IFS=$'\t' read -r _sl_title _sl_url; do
      [ -z "$_sl_url" ] && continue
      python3 "$JARVIS_DIR/tasks/watchlater_save.py" "$_sl_title" "$_sl_url" "natural" \
        2>>"$LOG_FILE" >/dev/null || true
      log_info "[$session_id] watchlater saved: $_sl_title"
    done <<< "$_sl_extracted"
    # Strip markers from reply
    reply=$(echo "$reply" | sed 's/\[SAVE_LATER:[^]]*\]//g' | sed '/^[[:space:]]*$/d')
  fi

  log_info "[$session_id] Replied (${#reply} chars)"

  # Remove the "working on it" reaction and send the real reply
  [ -n "$reaction_id" ] && lark_remove_reaction "$message_id" "$reaction_id"
  if ! lark_reply "$message_id" "$reply"; then
    log_err "[$session_id] Failed to send reply to Lark"
  fi
}

# ── Background Job Runner ────────────────────────────────────────────
# Runs a Claude task in an independent session, notifies on completion.
run_background_job() {
  local job_id="$1" conv_key="$2" content="$3" message_id="$4"
  local bg_session_id="bg-${job_id}"
  local output_file="$JOBS_DIR/${job_id}/output.md"
  local log_file_job="$JOBS_DIR/${job_id}/log.txt"

  # Build a minimal system prompt for the background job
  local memory now_ts sys_prompt
  memory=$(load_memory)
  now_ts=$(date '+%Y-%m-%d %H:%M %A')

  sys_prompt="You are running as a background job. Complete the task thoroughly.
When done, provide a clear summary of results.
Current time: $now_ts

$memory"

  # Run Claude with independent session
  (cd "$WORK_DIR" && with_timeout 3600 claude -p "$content" \
    --session-id "$bg_session_id" \
    --append-system-prompt "$sys_prompt" \
    --dangerously-skip-permissions \
    < /dev/null 2>>"$log_file_job" > "$output_file" || true) &
  local _bg_pid=$!

  # Record PID in registry
  JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" set-pid "$job_id" "$_bg_pid" \
    2>>"$LOG_FILE" || true

  # Wait for completion
  wait $_bg_pid 2>/dev/null
  local exit_code=$?

  # Read output
  local output=""
  [ -f "$output_file" ] && output=$(cat "$output_file" 2>/dev/null)

  # Determine status
  local status="completed"
  if [ -z "$output" ] || looks_like_error "$output"; then
    status="failed"
  fi

  # Check if job was cancelled (registry may have been updated)
  local current_status
  current_status=$(JV_JOBS_DIR="$JOBS_DIR" python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.jobs import JobManager
j = JobManager('$JOBS_DIR').get_job('$job_id')
print(j['status'] if j else 'unknown')
" 2>/dev/null || echo "unknown")

  if [ "$current_status" = "cancelled" ]; then
    log_info "[bg:$job_id] Job was cancelled"
    return
  fi

  # Update registry
  JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" finish "$job_id" "$status" \
    2>>"$LOG_FILE" || true

  log_info "[bg:$job_id] Finished with status=$status (${#output} chars)"

  # Notify user via card
  local card_body card_json
  if [ "$status" = "completed" ]; then
    # Truncate output for notification (full output available via 'job output')
    local summary
    if [ ${#output} -gt 3000 ]; then
      summary="${output:0:3000}

... (truncated, send 'job output $job_id' for full result)"
    else
      summary="$output"
    fi
    card_body="**Job completed** \`$job_id\`

$summary"
  else
    card_body="**Job failed** \`$job_id\`
Task: $content

Check logs with: job output $job_id"
  fi
  card_json=$(JV_BODY="$card_body" python3 -c "
import os, sys; sys.path.insert(0, '$JARVIS_DIR')
from core.card import build_card
print(build_card('⚙️ 后台任务', os.environ['JV_BODY']))
" 2>/dev/null) || card_json=""
  if [ -n "$card_json" ]; then
    lark_send_card "$card_json"
  else
    send_to_lark "$card_body"
  fi
}

# Cleanup on exit
cleanup() {
  log_info "Shutting down..."
  rm -f "$PIDFILE"
  [ -n "$ADMIN_PID" ] && kill "$ADMIN_PID" 2>/dev/null || true
  [ -n "$STREAM_PID" ] && kill "$STREAM_PID" 2>/dev/null || true
  kill "$HEARTBEAT_PID" 2>/dev/null || true
  # Kill any lingering eigenflux stream subprocesses
  pkill -P "$STREAM_PID" 2>/dev/null || true
  # Kill all background message handlers and jobs
  jobs -p 2>/dev/null | xargs -r kill 2>/dev/null || true
  wait "$HEARTBEAT_PID" 2>/dev/null || true
  [ -n "$STREAM_PID" ] && wait "$STREAM_PID" 2>/dev/null || true
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

      # ── Card action callback (e.g. watchlater button) ──────────────
      _card_action=$(echo "$line" | jq -r '.action.value.action // empty' 2>/dev/null)
      if [ "$_card_action" = "watchlater" ]; then
        _wl_title=$(echo "$line" | jq -r '.action.value.title // empty' 2>/dev/null)
        _wl_url=$(echo "$line" | jq -r '.action.value.url // empty' 2>/dev/null)
        if [ -n "$_wl_url" ]; then
          _wl_result=$(python3 "$JARVIS_DIR/tasks/watchlater_save.py" "$_wl_title" "$_wl_url" "button" 2>>"$LOG_FILE")
          log_info "[watchlater] Saved via button: $_wl_title"
          # Card action callbacks can return a toast; for now just log
        fi
        continue
      fi

      # Check for restart trigger (written by admin panel)
      if [ -f "$JARVIS_DIR/.restart_trigger" ]; then
        rm -f "$JARVIS_DIR/.restart_trigger"
        log_info "Restart triggered — exec-ing self..."
        exec "$JARVIS_DIR/bot.sh"
      fi

      content=$(echo "$line" | jq -r '.content // empty' 2>/dev/null)
      message_id=$(echo "$line" | jq -r '.message_id // empty' 2>/dev/null)
      chat_type=$(echo "$line" | jq -r '.chat_type // empty' 2>/dev/null)
      chat_id=$(echo "$line" | jq -r '.chat_id // empty' 2>/dev/null)
      sender_id=$(echo "$line" | jq -r '.sender_id // empty' 2>/dev/null)
      msg_type=$(echo "$line" | jq -r '.msg_type // empty' 2>/dev/null)

      # Log every received event for debugging (even if we skip it)
      if [ -n "$message_id" ]; then
        log_info "Event: msg_type=${msg_type:-text} content_len=${#content} mid=${message_id} chat_type=${chat_type} content_head=${content:0:80}"
      fi

      [ -z "$content" ] || [ -z "$message_id" ] && continue

      # ── Image detection: compact mode sends images as [Image: img_v3_xxx] ──
      if [[ "$content" == "[Image: img_v3_"* ]]; then
        _img_key=$(echo "$content" | sed 's/\[Image: \(.*\)\]/\1/')
        _img_dir="$JARVIS_DIR/tmp/images"
        mkdir -p "$_img_dir"
        _img_path="$_img_dir/${_img_key}.png"
        log_info "Image detected: $_img_key — downloading..."
        if (cd "$_img_dir" && lark-cli im +messages-resources-download \
            --message-id "$message_id" \
            --file-key "$_img_key" \
            --type image \
            --output "${_img_key}.png" \
            --as bot 2>>"$LOG_FILE"); then
          content="[User sent an image, saved to $_img_path. Use the Read tool to view it and reply about its content.]"
          log_info "Image downloaded: $_img_path"
        else
          content="[User sent an image (key: $_img_key) but download failed. Tell the user the image could not be received.]"
          log_warn "Image download failed: $_img_key"
        fi
      fi

      # In group chats, only respond when the bot is @mentioned.
      # Check if APP_ID appears anywhere in the mentions JSON — this is the
      # reliable way to detect a bot mention regardless of display name.
      if [ "$chat_type" != "p2p" ] && [ -n "$APP_ID" ]; then
        mentions_raw=$(echo "$line" | jq -r '.mentions // ""' 2>/dev/null)
        if ! echo "$mentions_raw" | grep -q "$APP_ID" 2>/dev/null; then
          log_info "Group message without @mention — ignoring"
          continue
        fi
      fi

      # Determine conv_key early (needed by most commands)
      if [ "$chat_type" = "p2p" ]; then
        conv_key="$sender_id"
      else
        conv_key="$chat_id"
      fi

      # Handle special commands (these run inline, NOT dispatched to background)
      # ONLY stop/cancel bypass Claude — everything else goes through LLM + action markers
      content_lower=$(echo "$content" | tr '[:upper:]' '[:lower:]')

      # "stop" / "cancel" — kill the running Claude process for this session (safety bypass)
      if [ "$content_lower" = "stop" ] || [ "$content_lower" = "cancel" ]; then
        _stop_sid=$(JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" python3 -c "
import json, os
try:
    print(json.load(open(os.environ['JV_TRACKER'])).get(os.environ['JV_KEY'], {}).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
        _stop_lock="$JARVIS_DIR/.session_lock_${_stop_sid}"
        if [ -f "$_stop_lock" ]; then
          _stop_pid=$(cat "$_stop_lock" 2>/dev/null)
          if [ -n "$_stop_pid" ] && kill -0 "$_stop_pid" 2>/dev/null; then
            pkill -TERM -P "$_stop_pid" 2>/dev/null || true
            kill "$_stop_pid" 2>/dev/null || true
            sleep 1
            if kill -0 "$_stop_pid" 2>/dev/null; then
              pkill -KILL -P "$_stop_pid" 2>/dev/null || true
              kill -KILL "$_stop_pid" 2>/dev/null || true
            fi
            log_info "[$_stop_sid] Killed by user (PID $_stop_pid)"
          fi
          rm -f "$_stop_lock"
          lark_reply_text "$message_id" "Stopped. Session is free now." >/dev/null
        else
          lark_reply_text "$message_id" "Nothing running." >/dev/null
        fi
        continue
      fi

      # ── Normal message → dispatch to background handler ──────────────
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

      # ── Engagement tracking: check if this message responds to a heartbeat ──
      python3 -c "
import json, time, os, sys

log_path = os.path.join('$JARVIS_DIR', 'engagement_log.jsonl')
if not os.path.exists(log_path):
    sys.exit(0)

# Read last 'sent' entry
last_sent = None
with open(log_path, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get('type') == 'sent':
                last_sent = entry
        except json.JSONDecodeError:
            continue

if not last_sent:
    sys.exit(0)

sent_epoch = last_sent.get('epoch', 0)
now_epoch = int(time.time())
gap_seconds = now_epoch - sent_epoch

# Only track if sent within last 60 minutes
if gap_seconds > 3600:
    sys.exit(0)

# Classify: engaged (<10min), late (10-30min), ignored (>30min)
if gap_seconds <= 600:
    reaction = 'engaged'
elif gap_seconds <= 1800:
    reaction = 'late_reply'
else:
    reaction = 'ignored'

entry = {
    'ts': time.strftime('%Y-%m-%d %H:%M'),
    'source': last_sent.get('source', 'unknown'),
    'type': 'response',
    'reaction': reaction,
    'gap_seconds': gap_seconds,
    'content_head': sys.argv[1][:80] if len(sys.argv) > 1 else '',
}
with open(log_path, 'a', encoding='utf-8') as f:
    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
" "$content" 2>>"$LOG_FILE" || true

      # Add a reaction to indicate we're working on it
      reaction_result=$(lark_add_reaction "$message_id" "Typing")
      reaction_id=$(echo "$reaction_result" | jq -r '.reaction_id // .data.reaction_id // empty' 2>/dev/null || true)

      # Concurrency guard: wait if too many handlers are running
      while [ "$(jobs -r 2>/dev/null | wc -l)" -ge "$MAX_HANDLERS" ]; do
        sleep 1
      done

      # Dispatch to background — main loop continues immediately
      handle_message "$conv_key" "$content" "$message_id" "$session_id" "$reaction_id" &
      log_info "[$session_id] Dispatched to background handler (PID $!)"
    done
