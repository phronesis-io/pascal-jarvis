#!/usr/bin/env bash
# jarvis-harness: Lark/Feishu bot + heartbeat-driven personal AI agent
#
# - Listens for user messages on Lark
# - Heartbeat loop runs scheduled tasks (feed triage, memory consolidation, etc.)
# - Claude Code handles conversation with full memory injection
#
# Configuration: jarvis.yaml (copy from jarvis.example.yaml)

set -uo pipefail

# Ensure UTF-8 encoding for Chinese content processing
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
export JARVIS_DIR

# ── Single-instance lock (prevent duplicate replies) ────────────────
# PID file format: "PID BOOT_TIMESTAMP" — validates both PID liveness AND
# that the PID belongs to the same boot cycle (guards against PID reuse).
PIDFILE="$JARVIS_DIR/.bot.pid"
_BOOT_TS=$(date +%s)
if [ -f "$PIDFILE" ]; then
  old_pid=$(awk '{print $1}' "$PIDFILE" 2>/dev/null)
  old_ts=$(awk '{print $2}' "$PIDFILE" 2>/dev/null)
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    # Extra check: if PID file is older than 7 days, it's likely a reused PID
    if [ -n "$old_ts" ] && [ "$(($(date +%s) - old_ts))" -lt 604800 ]; then
      echo "bot.sh is already running (PID $old_pid, started $(( ($(date +%s) - old_ts) / 60 ))m ago). Exiting." >&2
      exit 1
    fi
    # PID file too old — likely stale (PID reused by another process)
    echo "WARN: Stale PID file (PID $old_pid, age > 7 days) — overriding" >&2
  fi
  rm -f "$PIDFILE"
fi
echo "$$ $_BOOT_TS" > "$PIDFILE"

# ── Process conflict detection ──────────────────────────────────────
# Detect competing eigenflux stream processes from openclaw-gateway or
# stale bot instances. Multiple streams cause "Connection replaced" loops.
_competing_streams=$(pgrep -f "eigenflux stream" 2>/dev/null | wc -l | tr -d ' ')
if [ "$_competing_streams" -gt 0 ]; then
  echo "WARN: Found $_competing_streams competing eigenflux stream process(es) — killing to prevent Connection replaced loop" >&2
  pkill -f "eigenflux stream" 2>/dev/null || true
  sleep 1
fi

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

export MEMORY_DIR WORK_DIR CLAUDE_PROJECT_DIR USER_ID LOG_FILE HEARTBEAT_MODEL CHECK_INTERVAL

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
         "$JOBS_DIR" "$JARVIS_DIR/session_compacts"
[ -f "$SESSION_TRACKER" ] || echo "{}" > "$SESSION_TRACKER"

# Clean up stale session locks from previous crashes/restarts
rm -f "$JARVIS_DIR"/.session_lock_* 2>/dev/null

# Clean up old Claude session files (>30 days, prevents unbounded disk growth)
if [ -d "$CLAUDE_PROJECT_DIR" ]; then
  _old_sessions=$(find "$CLAUDE_PROJECT_DIR" -name "*.jsonl" -mtime +30 2>/dev/null | wc -l | tr -d ' ')
  if [ "$_old_sessions" -gt 0 ]; then
    find "$CLAUDE_PROJECT_DIR" -name "*.jsonl" -mtime +30 -delete 2>/dev/null
    log_info "Cleaned $_old_sessions session files older than 30 days"
  fi
fi

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
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.memory import load_tiered_memory
print(load_tiered_memory(os.environ['MEMORY_DIR']))
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
sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.safety import looks_like_error
sys.exit(0 if looks_like_error(os.environ.get('JV_TEXT','')) else 1)
" 2>/dev/null
}

# ── Heartbeat Loop (background, Python) ─────────────────────────────
# The heartbeat loop is now in Python (core/heartbeat_loop.py) where it
# can be tested. bot.sh only launches it as a background process.
# All output routing, card/text splitting, outbox writing, engagement
# tracking, and restart detection is in Python — no more bash set -u traps.
# ── Replay dropped messages from restart ────────────────────────────
# If a restart killed in-flight handlers, notify the user so they know
# to resend. We can't replay the original content (it's lost with the
# process), but we CAN tell them which sessions were interrupted.
_queue_file="$JARVIS_DIR/.message_queue"
if [ -f "$_queue_file" ] && [ -s "$_queue_file" ] && [ -n "$USER_ID" ]; then
  _dropped=$(wc -l < "$_queue_file" | tr -d ' ')
  log_warn "Found $_dropped interrupted message(s) from restart — notifying user"
  source "$JARVIS_DIR/plugins/lark/client.sh" 2>/dev/null || true
  lark_send_text "$USER_ID" \
    "⚠️ 重启中断了 ${_dropped} 条正在处理的消息，请重新发送。" >/dev/null 2>&1 || true
  rm -f "$_queue_file"
fi

sleep 3  # let config load settle
python3 -m core.heartbeat_loop 2>>"$LOG_FILE" &
HEARTBEAT_PID=$!

# ── EigenFlux Real-Time Stream (background, Python) ─────────────────
# The stream loop is now in Python (core/ef_stream_loop.py) where it
# can be tested. Handles reconnect, backoff, message delivery, analysis.
export PATH="$HOME/.local/bin:$PATH"
LOG_FILE="$LOG_FILE" python3 -m core.ef_stream_loop 2>>"$LOG_FILE" &
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

  # ── Python-handled actions (calendar, task, intent, feed, watchlater, etc.) ──
  # Delegate to core/actions.py for all non-process-control actions.
  # This handles: feed_search, watchlater, heartbeat, calendar_*, task_*,
  # praxis_*, intent_*, schedule_task. Returns cleaned reply with results.
  reply=$(JV_REPLY="$reply" JV_LOG_FILE="$LOG_FILE" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.actions import ActionProcessor
ap = ActionProcessor(os.environ['JARVIS_DIR'], os.environ['MEMORY_DIR'],
                     os.environ.get('JV_JOBS_DIR', 'jobs'), os.environ.get('JV_LOG_FILE', ''))
print(ap.process(os.environ['JV_REPLY']))
" 2>>"$LOG_FILE" || printf '%s' "$reply")

  # ── Bash-handled actions (need process control: &, wait, PID tracking) ──
  # Re-extract remaining markers (Python already stripped the ones it handled)
  actions=$(echo "$reply" | grep -o '\[ACTION:[^]]*\]' 2>/dev/null || true)

  if [ -z "$actions" ]; then
    printf '%s' "$reply"
    return
  fi

  while IFS= read -r marker; do
    [ -z "$marker" ] && continue
    local action_body="${marker#\[ACTION:}"
    action_body="${action_body%\]}"
    local action_type="${action_body%%|*}"
    local action_params="${action_body#*|}"
    [ "$action_params" = "$action_type" ] && action_params=""

    case "$action_type" in
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

      # All other action types (heartbeat, calendar_*, task_*, praxis_*, intent_*, etc.)
      # are handled by core/actions.py above. If any marker reaches here, it's unknown.
      *)
        log_warn "[action] Unknown action type in bash fallback: $action_type"
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

  # Build system prompt with memory + recent turns (delegated to core/prompt.py)
  local sys_prompt
  sys_prompt=$(JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" \
    JV_SID="$session_id" JV_SDIR="$CLAUDE_PROJECT_DIR" \
    python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.prompt import build_system_prompt
from core.timeutil import now_local_str
print(build_system_prompt(
    jarvis_dir=os.environ['JARVIS_DIR'],
    memory_dir=os.environ['MEMORY_DIR'],
    session_dir=os.environ.get('JV_SDIR', ''),
    session_id=os.environ.get('JV_SID', ''),
    conv_key=os.environ.get('JV_KEY', ''),
    now_ts=now_local_str('%Y-%m-%d %H:%M %A'),
    tracker_path=os.environ.get('JV_TRACKER', 'active_sessions.json'),
))
" 2>>"$LOG_FILE")

  if [ -z "$sys_prompt" ]; then
    log_warn "[$session_id] System prompt build failed — using fallback"
    sys_prompt="You are a personal assistant. Reply in the same language the user uses.
$(load_memory)"
  fi

  # Call Claude (runs in WORK_DIR, with optional timeout)
  # Wait for any existing session lock (previous message still processing)
  local LOCK_FILE="$JARVIS_DIR/.session_lock_${session_id}"
  local ANSWER_FILE
  ANSWER_FILE=$(mktemp /tmp/jarvis-answer-XXXXXX)
  local waited=0
  while [ -f "$LOCK_FILE" ] && [ "$waited" -lt 620 ]; do
    # Wait up to 620s (slightly longer than Claude's 600s timeout)
    # to avoid concurrent access to the same session
    if [ "$((waited % 30))" -eq 0 ] && [ "$waited" -gt 0 ]; then
      log_info "[$session_id] Session busy, waiting... (${waited}s)"
    fi
    sleep 5
    waited=$((waited + 5))
  done
  if [ "$waited" -ge 620 ]; then
    # Previous handler likely timed out but didn't clean up its lock
    log_warn "[$session_id] Lock wait timeout (${waited}s) — clearing stale lock"
    rm -f "$LOCK_FILE"
  fi

  # Run Claude with automatic retry on empty response
  local session_file="$CLAUDE_PROJECT_DIR/${session_id}.jsonl"
  local _claude_pid
  local answer=""
  local _attempt

  for _attempt in 1 2; do
    if [ "$_attempt" -eq 2 ]; then
      log_info "[$session_id] Retry attempt 2 after empty response (sleeping 3s)"
      sleep 3
    fi

    if [ -f "$session_file" ]; then
      [ "$_attempt" -eq 1 ] && log_info "[$session_id] Resuming session"
      (trap 'echo "$(date +%H:%M:%S) SIGTERM in subshell pid=$$ sid=$session_id" >> /tmp/claude_subshell_signals.log' TERM
       cd "$WORK_DIR" && printf '%s' "$content" | claude -p \
        --resume "$session_id" \
        --append-system-prompt "$sys_prompt" \
        --dangerously-skip-permissions \
        2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
    else
      [ "$_attempt" -eq 1 ] && log_info "[$session_id] New session"
      (trap 'echo "$(date +%H:%M:%S) SIGTERM in subshell pid=$$ sid=$session_id" >> /tmp/claude_subshell_signals.log' TERM
       cd "$WORK_DIR" && printf '%s' "$content" | claude -p \
        --session-id "$session_id" \
        --append-system-prompt "$sys_prompt" \
        --dangerously-skip-permissions \
        2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
    fi
    _claude_pid=$!
    echo "$_claude_pid" > "$LOCK_FILE"
    wait $_claude_pid 2>/dev/null
    local _exit_code=$?

    answer=$(cat "$ANSWER_FILE" 2>/dev/null)
    local _stderr_content
    _stderr_content=$(head -5 "${ANSWER_FILE}.stderr" 2>/dev/null | tr '\n' ' ')

    if [ -z "$answer" ]; then
      log_warn "[$session_id] Empty answer from Claude (attempt $_attempt, exit=$_exit_code, stderr=${_stderr_content:-none})"
      # On first failure, session file may have been created — update for retry
      session_file="$CLAUDE_PROJECT_DIR/${session_id}.jsonl"
    else
      break
    fi
  done

  rm -f "$ANSWER_FILE" "${ANSWER_FILE}.stderr" "$LOCK_FILE"

  # Filter error-like answers — never send them to the user as the "real" reply
  local reply=""
  if [ -n "$answer" ] && ! looks_like_error "$answer"; then
    reply="$answer"
  fi

  if [ -z "$reply" ]; then
    log_warn "[$session_id] Final empty/error answer from Claude (${#answer} chars after 2 attempts)"
    if [ -n "$answer" ]; then
      log_warn "[$session_id] Suppressed content: ${answer:0:500}"
    fi
    [ -n "$reaction_id" ] && lark_remove_reaction "$message_id" "$reaction_id"
    # Tell user exactly what happened — not a vague "try again"
    if [ "${#answer}" -eq 0 ]; then
      lark_reply_text "$message_id" \
        "Claude 连续两次返回空响应（API 可能暂时不稳定）。请稍后重试。" >/dev/null
    else
      lark_reply_text "$message_id" \
        "Claude 的回复被安全过滤器拦截了（可能包含错误信息）。请换个方式重试。" >/dev/null
    fi
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
        2>>"$LOG_FILE" >/dev/null || log_warn "[watchlater] Save failed: $_sl_title"
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
    2>>"$LOG_FILE" || log_warn "[bg:$job_id] Failed to register PID"

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
  current_status=$(JV_JOBS_DIR="$JOBS_DIR" JV_JOB_ID="$job_id" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.jobs import JobManager
j = JobManager(os.environ['JV_JOBS_DIR']).get_job(os.environ['JV_JOB_ID'])
print(j['status'] if j else 'unknown')
" 2>/dev/null || echo "unknown")

  if [ "$current_status" = "cancelled" ]; then
    log_info "[bg:$job_id] Job was cancelled"
    return
  fi

  # Update registry
  JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" finish "$job_id" "$status" \
    2>>"$LOG_FILE" || log_warn "[bg:$job_id] Failed to update job status"

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
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
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
  [ -n "$WATCHDOG_PID" ] && kill "$WATCHDOG_PID" 2>/dev/null || true
  kill "$HEARTBEAT_PID" 2>/dev/null || true
  # Kill any lingering eigenflux stream processes (may be reparented to
  # openclaw-gateway or init, so pkill -P doesn't reach them)
  pkill -f "eigenflux stream" 2>/dev/null || true
  # Kill all background message handlers and jobs
  jobs -p 2>/dev/null | xargs -r kill 2>/dev/null || true
  wait "$HEARTBEAT_PID" 2>/dev/null || true
  [ -n "$STREAM_PID" ] && wait "$STREAM_PID" 2>/dev/null || true
  [ -n "$ADMIN_PID" ] && wait "$ADMIN_PID" 2>/dev/null || true
  log_info "Stopped."
}
trap cleanup EXIT INT TERM

# ── Heartbeat Watchdog (background) ──────────────────────────────────
# Separate loop that checks heartbeat PID every 30s and restarts if dead.
# Can't rely on Lark events (they may not arrive) or daemon (30min delay).
heartbeat_watchdog() {
  sleep 30  # initial grace period
  while true; do
    if ! kill -0 "$HEARTBEAT_PID" 2>/dev/null; then
      log_warn "[watchdog] Heartbeat PID $HEARTBEAT_PID died — restarting"
      heartbeat_loop &
      HEARTBEAT_PID=$!
      log_info "[watchdog] Heartbeat restarted (PID: $HEARTBEAT_PID)"
    fi
    sleep 30
  done
}
heartbeat_watchdog &
WATCHDOG_PID=$!

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

      # ── Card action callback (e.g. watchlater button, feedback) ──────
      # Debug: log raw card action events to diagnose button click issues
      _has_action=$(echo "$line" | jq -r '.action // .event.action // empty' 2>/dev/null)
      if [ -n "$_has_action" ] && [ "$_has_action" != "null" ]; then
        log_info "[card-action] Raw event: ${line:0:200}"
      fi
      _card_action=$(echo "$line" | jq -r '.action.value.action // .event.action.value.action // empty' 2>/dev/null)
      if [ "$_card_action" = "feedback" ]; then
        _fb_source=$(echo "$line" | jq -r '.action.value.source // .event.action.value.source // empty' 2>/dev/null)
        _fb_rating=$(echo "$line" | jq -r '.action.value.rating // .event.action.value.rating // empty' 2>/dev/null)
        if [ -n "$_fb_source" ] && [ -n "$_fb_rating" ]; then
          _fb_ts=$(date '+%Y-%m-%d %H:%M')
          printf '%s\n' "{\"ts\":\"$_fb_ts\",\"source\":\"$_fb_source\",\"type\":\"feedback\",\"rating\":\"$_fb_rating\",\"epoch\":$(date +%s)}" \
            >> "$JARVIS_DIR/engagement_log.jsonl"
          log_info "[feedback] $_fb_source: $_fb_rating"
        fi
        continue
      fi
      if [ "$_card_action" = "watchlater" ]; then
        _wl_title=$(echo "$line" | jq -r '.action.value.title // .event.action.value.title // empty' 2>/dev/null)
        _wl_url=$(echo "$line" | jq -r '.action.value.url // .event.action.value.url // empty' 2>/dev/null)
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
        log_info "Restart triggered from message loop — cleaning up and exec-ing self..."
        # Save pending messages so they can be replayed after restart
        _queue_file="$JARVIS_DIR/.message_queue"
        rm -f "$_queue_file"
        for _lock in "$JARVIS_DIR"/.session_lock_*; do
          [ -f "$_lock" ] || continue
          _lock_sid=$(basename "$_lock" | sed 's/^\.session_lock_//')
          log_info "Queuing in-flight message for session $_lock_sid"
          # We can't recover the content, but we can notify the user
          echo "$_lock_sid" >> "$_queue_file"
        done
        # Kill background children before exec to prevent orphan processes
        kill 0 2>/dev/null || true
        exec "$JARVIS_DIR/bot.sh"
      fi

      # Parse message fields from raw (non-compact) event format.
      # Raw format: .event.message.content is a JSON string like '{"text":"hello"}'
      # We extract .text from that inner JSON to get plain text.
      _raw_content=$(echo "$line" | jq -r '.content // .event.message.content // empty' 2>/dev/null)
      # Extract text from content JSON wrapper. Handles:
      # - Plain text: {"text":"hello"} → hello
      # - Rich text (post): {"title":"","content":[[{"tag":"text","text":"..."}]]} → concatenated text
      content=$(echo "$_raw_content" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    if isinstance(d, str):
        print(d)  # already plain text
    elif 'text' in d:
        print(d['text'])  # plain text message
    elif 'content' in d:
        # Rich text: extract all text tags
        parts = []
        for block in d.get('content', []):
            for item in (block if isinstance(block, list) else [block]):
                if isinstance(item, dict) and item.get('text'):
                    parts.append(item['text'])
        title = d.get('title', '')
        if title: parts.insert(0, title)
        print(' '.join(parts))
    else:
        print(json.dumps(d))
except:
    print(sys.stdin.read() if hasattr(sys.stdin, 'read') else '')
" 2>/dev/null)
      # Fallback: if Python fails, use raw content
      [ -z "$content" ] && content="$_raw_content"
      message_id=$(echo "$line" | jq -r '.message_id // .event.message.message_id // empty' 2>/dev/null)
      chat_type=$(echo "$line" | jq -r '.chat_type // .event.message.chat_type // empty' 2>/dev/null)
      chat_id=$(echo "$line" | jq -r '.chat_id // .event.message.chat_id // empty' 2>/dev/null)
      sender_id=$(echo "$line" | jq -r '.sender_id // .event.sender.sender_id.open_id // empty' 2>/dev/null)
      msg_type=$(echo "$line" | jq -r '.msg_type // .event.message.message_type // empty' 2>/dev/null)

      # Log every received event for debugging (even if we skip it)
      if [ -n "$message_id" ]; then
        log_info "Event: msg_type=${msg_type:-text} content_len=${#content} mid=${message_id} chat_type=${chat_type} content_head=${content:0:80}"
      fi

      [ -z "$content" ] || [ -z "$message_id" ] && continue

      # ── Dedup: skip if same message_id seen within 10s (Lark sometimes delivers twice) ──
      _dedup_file="/tmp/jarvis-last-msg"
      _dedup_key="${message_id}"
      if [ -f "$_dedup_file" ] && [ "$(cat "$_dedup_file" 2>/dev/null)" = "$_dedup_key" ]; then
        log_info "Duplicate message skipped: $message_id"
        continue
      fi
      printf '%s' "$_dedup_key" > "$_dedup_file"

      # ── Non-text message dispatch (image / file / audio / sticker / share / location / etc) ──
      # In raw (non-compact) mode, content is a JSON string like {"image_key":"...","file_key":"..."}
      # Parse based on msg_type from the event envelope.
      case "$msg_type" in
        image)
          _img_key=$(echo "$_raw_content" | jq -r '.image_key // empty' 2>/dev/null)
          if [ -n "$_img_key" ]; then
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
          ;;
        file)
          _file_key=$(echo "$_raw_content" | jq -r '.file_key // empty' 2>/dev/null)
          _file_name=$(echo "$_raw_content" | jq -r '.file_name // empty' 2>/dev/null)
          if [ -n "$_file_key" ]; then
            _file_dir="$JARVIS_DIR/tmp/files"
            mkdir -p "$_file_dir"
            # Sanitize file_name to avoid path traversal; fallback to file_key
            _file_safe=$(echo "${_file_name:-$_file_key}" | tr -d '/\\' | head -c 200)
            [ -z "$_file_safe" ] && _file_safe="$_file_key"
            _file_path="$_file_dir/$_file_safe"
            log_info "File detected: $_file_key ($_file_name) — downloading..."
            if (cd "$_file_dir" && lark-cli im +messages-resources-download \
                --message-id "$message_id" \
                --file-key "$_file_key" \
                --type file \
                --output "$_file_safe" \
                --as bot 2>>"$LOG_FILE"); then
              content="[User sent a file: ${_file_name:-(unnamed)}, saved to $_file_path. Use the Read tool to view its contents if relevant to the conversation.]"
              log_info "File downloaded: $_file_path"
            else
              content="[User sent a file: ${_file_name:-$_file_key} but download failed.]"
              log_warn "File download failed: $_file_key"
            fi
          fi
          ;;
        audio)
          _audio_dur=$(echo "$_raw_content" | jq -r '.duration // empty' 2>/dev/null)
          _audio_key=$(echo "$_raw_content" | jq -r '.file_key // empty' 2>/dev/null)
          if [ -n "$_audio_key" ]; then
            _audio_dir="$JARVIS_DIR/tmp/audios"
            mkdir -p "$_audio_dir"
            _audio_file="${message_id}.ogg"
            _audio_path="$_audio_dir/$_audio_file"
            log_info "Audio detected: $_audio_key (${_audio_dur:-?}ms) — downloading..."
            if (cd "$_audio_dir" && lark-cli im +messages-resources-download \
                --message-id "$message_id" \
                --file-key "$_audio_key" \
                --type file \
                --output "$_audio_file" \
                --as bot 2>>"$LOG_FILE"); then
              if [ -n "$OPENAI_API_KEY" ] && command -v curl >/dev/null 2>&1; then
                log_info "Transcribing audio via Whisper API..."
                _transcript=$(curl -sS --max-time 60 \
                  -X POST https://api.openai.com/v1/audio/transcriptions \
                  -H "Authorization: Bearer $OPENAI_API_KEY" \
                  -F "file=@$_audio_path" \
                  -F "model=whisper-1" \
                  -F "language=zh" 2>>"$LOG_FILE" | jq -r '.text // empty' 2>/dev/null)
                if [ -n "$_transcript" ]; then
                  content="[User sent a voice message (${_audio_dur:-?}ms). Transcript: $_transcript]"
                  log_info "Audio transcribed: ${_transcript:0:60}..."
                else
                  content="[User sent a voice message (${_audio_dur:-?}ms), saved to $_audio_path. Whisper transcription returned empty — ask the user to type if needed.]"
                  log_warn "Whisper API returned empty transcript"
                fi
              else
                content="[User sent a voice message (${_audio_dur:-?}ms), saved to $_audio_path. Transcription needs OPENAI_API_KEY (not configured) — ask the user to type the content.]"
                log_info "Audio downloaded but OPENAI_API_KEY not set"
              fi
            else
              content="[User sent a voice message but download failed.]"
              log_warn "Audio download failed: $_audio_key"
            fi
          else
            content="[User sent a voice message but file_key was missing.]"
          fi
          ;;
        merge_forward)
          log_info "Merge forward detected — fetching content via mget"
          _mget_result=$(lark-cli im +messages-mget --message-ids "$message_id" --as bot 2>>"$LOG_FILE")
          _forward_content=$(echo "$_mget_result" | jq -r '.data.messages[0].content // empty' 2>/dev/null)
          if [ -n "$_forward_content" ]; then
            content="[User shared a merged-forward chat record (合并转发). Contents below:]
$_forward_content"
            log_info "Merge forward expanded ($(echo -n "$_forward_content" | wc -c | tr -d ' ') chars)"
          else
            content="[User shared a merged-forward chat record but couldn't fetch contents via mget.]"
            log_warn "Merge forward mget returned empty"
          fi
          ;;
        media)
          _media_name=$(echo "$_raw_content" | jq -r '.file_name // empty' 2>/dev/null)
          content="[User sent a video: ${_media_name:-(unnamed)}. Video processing is not yet supported — ask the user to describe it if relevant.]"
          log_info "Video message received (no processing)"
          ;;
        sticker)
          content="[User sent a sticker.]"
          log_info "Sticker received"
          ;;
        share_chat)
          _shared_chat=$(echo "$_raw_content" | jq -r '.chat_id // empty' 2>/dev/null)
          content="[User shared a group/chat card (chat_id: $_shared_chat).]"
          log_info "Chat card shared: $_shared_chat"
          ;;
        share_user)
          _shared_user=$(echo "$_raw_content" | jq -r '.user_id // .open_id // empty' 2>/dev/null)
          content="[User shared a contact card (user_id: $_shared_user). Use lark-cli contact to look them up if needed.]"
          log_info "User card shared: $_shared_user"
          ;;
        location)
          _loc_name=$(echo "$_raw_content" | jq -r '.name // empty' 2>/dev/null)
          _loc_lng=$(echo "$_raw_content" | jq -r '.longitude // empty' 2>/dev/null)
          _loc_lat=$(echo "$_raw_content" | jq -r '.latitude // empty' 2>/dev/null)
          content="[User shared a location: ${_loc_name:-(unnamed)} (lat=$_loc_lat, lng=$_loc_lng)]"
          log_info "Location shared: $_loc_name"
          ;;
        interactive)
          # Cards are structured JSON. Recursively extract any text-bearing fields.
          _card_text=$(echo "$_raw_content" | python3 -c "
import json, sys
TEXT_KEYS = {'text', 'plain_text', 'content', 'title', 'tag_name', 'value'}
def extract(d, parts, depth=0):
    if depth > 20: return
    if isinstance(d, dict):
        for k, v in d.items():
            if k in TEXT_KEYS and isinstance(v, str) and v.strip():
                parts.append(v.strip())
            else:
                extract(v, parts, depth+1)
    elif isinstance(d, list):
        for item in d:
            extract(item, parts, depth+1)
try:
    d = json.loads(sys.stdin.read())
    parts = []
    extract(d, parts)
    # Dedup adjacent duplicates and truncate
    seen = []
    for p in parts:
        if not seen or seen[-1] != p:
            seen.append(p)
    print(' | '.join(seen)[:1500])
except Exception as e:
    pass
" 2>/dev/null)
          if [ -n "$_card_text" ]; then
            content="[User sent an interactive card. Extracted text: $_card_text]"
          else
            content="[User sent an interactive card (no extractable text). Raw head: ${_raw_content:0:200}]"
          fi
          log_info "Interactive card received (text len=${#_card_text})"
          ;;
        # text and post are already extracted by the Python parser above — fall through.
      esac

      # In group chats, only respond when the bot is @mentioned.
      # Check if APP_ID appears anywhere in the mentions JSON — this is the
      # reliable way to detect a bot mention regardless of display name.
      if [ "$chat_type" != "p2p" ] && [ -n "$APP_ID" ]; then
        mentions_raw=$(echo "$line" | jq -r '.mentions // .event.message.mentions // ""' 2>/dev/null)
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
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.heartbeat import HeartbeatRunner
jd = os.environ['JARVIS_DIR']
runner = HeartbeatRunner(jd, os.path.join(jd, 'HEARTBEAT.md'),
    os.path.join(jd, 'heartbeat_state.json'), os.environ['MEMORY_DIR'],
    os.environ.get('HEARTBEAT_MODEL', 'sonnet'), work_dir=os.environ.get('WORK_DIR', jd))
runner.run_cycle(force=True, only_task='memory-hourly')
" 2>>"$LOG_FILE" >/dev/null || log_warn "Memory hourly on rotation failed"

        # Generate session compact synchronously — must complete before handle_message
        # reads it, otherwise the new session may see a partial/missing compact.
        JV_DIR="$JARVIS_DIR" JV_SDIR="$CLAUDE_PROJECT_DIR" JV_KEY="$conv_key" \
          JV_WORK="$WORK_DIR" python3 -c "
import sys, os, json
sys.path.insert(0, os.environ['JV_DIR'])
from core.compact import generate_compact, get_old_session_id
tracker = json.load(open(os.path.join(os.environ['JV_DIR'], 'active_sessions.json')))
counter = tracker.get(os.environ['JV_KEY'], {}).get('counter', 0)
old_sid = get_old_session_id(os.environ['JV_KEY'], counter)
if old_sid:
    generate_compact(os.environ['JV_DIR'], os.environ['JV_SDIR'],
                     old_sid, os.environ['JV_KEY'], os.environ['JV_WORK'])
" 2>>"$LOG_FILE" >/dev/null || log_warn "Session compact failed for $conv_key"
        log_info "Session compact completed for $conv_key"
      fi

      # Sanitize content for log (replace newlines/control chars to prevent log injection)
      _log_content=$(printf '%s' "$content" | tr '\n\r' '  ' | cut -c1-120)
      log_info "[$session_id] Received: $_log_content"

      # ── Engagement tracking: check if this message responds to a heartbeat ──
      python3 -m core.engagement "$content" 2>>"$LOG_FILE" || log_warn "Engagement tracking failed"

      # Add a reaction to indicate we're working on it
      reaction_result=$(lark_add_reaction "$message_id" "Typing")
      reaction_id=$(echo "$reaction_result" | jq -r '.reaction_id // .data.reaction_id // empty' 2>/dev/null || true)

      # Concurrency guard: wait if too many handlers are running
      # Note: `jobs -r` doesn't work reliably inside a pipe subshell.
      # Use /proc-style check: count active session lock files as a proxy.
      while [ "$(find "$JARVIS_DIR" -maxdepth 1 -name '.session_lock_*' 2>/dev/null | wc -l)" -ge "$MAX_HANDLERS" ]; do
        sleep 1
      done

      # Dispatch to background — main loop continues immediately
      handle_message "$conv_key" "$content" "$message_id" "$session_id" "$reaction_id" &
      log_info "[$session_id] Dispatched to background handler (PID $!)"
    done
