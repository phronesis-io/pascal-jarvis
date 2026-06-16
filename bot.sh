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

# Ensure the native-installer `claude` (~/.local/bin/claude → versions/<x>) is on
# PATH for EVERY child — most importantly the heartbeat loop, which shells out to
# `claude` each cycle. launchd starts the daemon with a minimal PATH that omits
# ~/.local/bin, so this MUST be exported before any child is spawned. Previously
# this lived further down (after the heartbeat launch), so on a launchd cold-start
# the heartbeat inherited the minimal PATH and every claude_call died with
# "Claude CLI not found", tripping non-priority circuits until the next restart.
export PATH="$HOME/.local/bin:$PATH"

# Anchor CWD to JARVIS_DIR. The bg helpers run as `python3 -m core.X`, which
# resolves `core/` from the CWD — if bot.sh is launched (or self-exec'd) from
# any other directory (e.g. a restart kicked off from WORK_DIR), every helper
# dies with `ModuleNotFoundError: No module named 'core'`, taking heartbeat and
# the ef-stream down and spiralling into a restart loop. Anchoring here makes
# that impossible regardless of how/where we were launched.
cd "$JARVIS_DIR" || { echo "FATAL: cannot cd to JARVIS_DIR ($JARVIS_DIR)" >&2; exit 1; }

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
# Match only actual eigenflux stream processes (not Claude prompts containing the string)
_competing_streams=$(ps -eo pid,comm,args | awk '$2 == "eigenflux" && $4 == "stream" {print $1}' | wc -l | tr -d ' ')
if [ "$_competing_streams" -gt 0 ]; then
  echo "WARN: Found $_competing_streams competing eigenflux stream process(es) — killing" >&2
  ps -eo pid,comm,args | awk '$2 == "eigenflux" && $4 == "stream" {print $1}' | xargs kill 2>/dev/null || true
  sleep 1
fi

LOG_FILE="$JARVIS_DIR/jarvis.log"
LOG_MAX_BYTES=500000  # 500KB — rotate on startup if exceeded
MEMORY_CACHE_FILE="$JARVIS_DIR/.memory_cache"   # last-known-good memory snapshot

# ── Log rotation (on startup) ────────────────────────────────────────
# Archive 3 generations before truncating — destroyed history made
# failure-rate audits impossible.
if [ -f "$LOG_FILE" ] && [ "$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
  mv -f "$LOG_FILE.2" "$LOG_FILE.3" 2>/dev/null || true
  mv -f "$LOG_FILE.1" "$LOG_FILE.2" 2>/dev/null || true
  cp -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
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
    # Pure-bash fallback. macOS ships no coreutils timeout, so this branch
    # used to run the command with NO limit — a placebo for every caller
    # (6000s bg jobs, 12s narration; a hung narration could wedge the
    # watchdog loop). Run in bg, kill past the deadline.
    # The killer's stdout MUST be detached: inside $(...) substitution it
    # would otherwise hold the pipe open for the full sleep.
    "$@" &
    local _wt_cmd_pid=$!
    ( sleep "$secs"; kill "$_wt_cmd_pid" 2>/dev/null ) >/dev/null 2>&1 &
    local _wt_killer_pid=$!
    wait "$_wt_cmd_pid"
    local _wt_rc=$?
    kill "$_wt_killer_pid" 2>/dev/null
    wait "$_wt_killer_pid" 2>/dev/null
    return $_wt_rc
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
# Event backend switch + sidecar credentials (jarvis.yaml is gitignored, so
# the secret never reaches the repo). Empty values keep the lark-cli path.
emit("JARVIS_EVENT_BACKEND", c.lark.get("event_backend", ""))
emit("LARK_APP_SECRET", c.lark.get("app_secret", ""))
emit("DATA_DIR", c.data_dir)
emit("WORK_DIR", c.work_dir)
emit("MEMORY_DIR", c.memory_dir)
emit("MAX_SESSION_SIZE", c.claude.get("max_session_size", 512000))
emit("MAIN_MODEL", c.claude.get("main_model", "opus") or "opus")
emit("HEARTBEAT_MODEL", c.claude.get("heartbeat_model", "opus"))
emit("HEARTBEAT_TIMEOUT", c.claude.get("heartbeat_timeout", 600))
emit("CLAUDE_BACKUP_ENABLED", str(bool(c.claude.get("backup_enabled", True))).lower())
emit("CLAUDE_BACKUP_AUTH_TOKEN", c.claude.get("backup_auth_token", ""))
emit("CLAUDE_BACKUP_BASE_URL", c.claude.get("backup_base_url", ""))
emit("OPENAI_FALLBACK_ENABLED", str(bool(c.openai.get("fallback_enabled", True))).lower())
emit("OPENAI_FALLBACK_MODEL", c.openai.get("fallback_model", "gpt-5.2"))
emit("OPENAI_API_KEY_CONFIG", c.openai.get("api_key", ""))
emit("OPENAI_BASE_URL", c.openai.get("base_url", "https://api.openai.com/v1"))
emit("OPENAI_USER_AGENT", c.openai.get("user_agent", ""))
emit("OPENAI_FALLBACK_TIMEOUT", c.openai.get("timeout", 120))
emit("OPENAI_FALLBACK_MAX_OUTPUT_TOKENS", c.openai.get("max_output_tokens", 4096))
emit("CHECK_INTERVAL", c.heartbeat.get("check_interval", 10))
emit("ADMIN_ENABLED", str(bool(c.admin.get("enabled", False))).lower())
emit("ADMIN_HOST", c.admin.get("host", "127.0.0.1"))
emit("ADMIN_PORT", c.admin.get("port", 3456))
PYEOF
)
# shellcheck disable=SC1090
eval "$CONFIG_VARS"

# Never let the main conversation inherit the account's DEFAULT claude model:
# that default can be a banned model (Fable). Pin to opus (= Opus 4.8) so an
# empty/missing config or a changed account default can never break the bot.
: "${MAIN_MODEL:=opus}"

# Claude Code stores sessions in ~/.claude/projects/<slug>/, where <slug>
# is the absolute cwd with every '/' replaced by '-' (leading dash kept).
CLAUDE_PROJECT_DIR="$HOME/.claude/projects/$(echo "$WORK_DIR" | sed 's|/|-|g')"
SESSION_TRACKER="$JARVIS_DIR/active_sessions.json"
HEARTBEAT_TRIGGER="/tmp/jarvis-heartbeat-trigger"

export MEMORY_DIR WORK_DIR CLAUDE_PROJECT_DIR USER_ID LOG_FILE MAIN_MODEL HEARTBEAT_MODEL HEARTBEAT_TIMEOUT CHECK_INTERVAL
export CLAUDE_BACKUP_ENABLED CLAUDE_BACKUP_AUTH_TOKEN CLAUDE_BACKUP_BASE_URL
export OPENAI_FALLBACK_ENABLED OPENAI_FALLBACK_MODEL OPENAI_BASE_URL OPENAI_USER_AGENT OPENAI_FALLBACK_TIMEOUT OPENAI_FALLBACK_MAX_OUTPUT_TOKENS
if [ -z "${OPENAI_API_KEY:-}" ] && [ -n "${OPENAI_API_KEY_CONFIG:-}" ]; then
  export OPENAI_API_KEY="$OPENAI_API_KEY_CONFIG"
fi
unset OPENAI_API_KEY_CONFIG
# Sidecar event backend (empty = lark-cli default; see plugins/lark/client.sh)
export JARVIS_EVENT_BACKEND LARK_APP_SECRET

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
# Delegates to core.session.SessionManager: the old embedded copy did an
# UNLOCKED, non-atomic read-modify-write of the tracker, racing
# force_rotate's flock+atomic writes (a concurrent rotation could be
# clobbered, and a torn write parsed as {} reset every counter). Same
# uuid5(NAMESPACE, key-counter) scheme, so session ids are unchanged.
get_session_id() {
  local conv_key="$1"
  JV_TRACKER="$SESSION_TRACKER" JV_SDIR="$CLAUDE_PROJECT_DIR" \
    JV_MAX="$MAX_SESSION_SIZE" JV_KEY="$conv_key" python3 <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["JARVIS_DIR"])
from core.session import SessionManager
sm = SessionManager(os.environ["JV_TRACKER"], os.environ["JV_SDIR"],
                    max_size=int(os.environ["JV_MAX"]))
sid, rotated = sm.get_session(os.environ["JV_KEY"])
if rotated:
    print("ROTATED", file=sys.stderr)
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
  lark_send "⚠️ 重启中断了 ${_dropped} 条正在处理的消息，请重新发送。" 2>/dev/null || true
  rm -f "$_queue_file"
fi

sleep 3  # let config load settle
python3 -m core.heartbeat_loop 2>>"$LOG_FILE" &
HEARTBEAT_PID=$!

# ── EigenFlux Real-Time Stream (background, Python) ─────────────────
# The stream loop is now in Python (core/ef_stream_loop.py) where it
# can be tested. Handles reconnect, backoff, message delivery, analysis.
# PATH (incl. ~/.local/bin) is exported at the top so every child — heartbeat,
# this stream, admin — can find the native-installer `claude`.
# Identify jarvis to EigenFlux server telemetry (same contract as client.sh).
# The `eigenflux stream` child inherits these via the Python process env.
EIGENFLUX_HOST="${EIGENFLUX_HOST:-jarvis}" EIGENFLUX_CHANNEL="${EIGENFLUX_CHANNEL:-lark}" \
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
          # Push a "started" card immediately so the user SEES the bg job kick
          # off (the completion card comes later from run_background_job).
          # Body via JV_BODY env to avoid shell expansion of backticks/$ —
          # same safe pattern as the completion card below.
          local _bg_start_body _bg_start_card
          _bg_start_body="正在后台独立运行，不占用我们的对话；跑完我会把结果卡片发给你。

**任务**：$bg_desc
**Job ID**：\`$job_id\`

查进度发「jobs」，查结果发「job output $job_id」，取消发「cancel $job_id」"
          _bg_start_card=$(JV_BODY="$_bg_start_body" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.card import build_card
print(build_card('🚀 后台任务已启动', os.environ['JV_BODY']))
" 2>>"$LOG_FILE") || _bg_start_card=""
          if [ -n "$_bg_start_card" ]; then
            lark_send_card "$_bg_start_card"
          else
            send_to_lark "🚀 后台任务已启动：$bg_desc （Job $job_id）"
          fi
          action_results="${action_results}
🚀 已在后台启动：$bg_desc"
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

  # Prepend an authoritative current-time line to the user's message body.
  # The system prompt already carries 'Current time', but the message body
  # itself has none — in a long all-day thread Claude can anchor on a stale
  # in-conversation timestamp. Single line at home (Shanghai), dual when abroad.
  local msg_ts
  msg_ts=$(python3 -c "import os,sys; sys.path.insert(0,os.environ['JARVIS_DIR']); from core.timeutil import msg_timestamp_prefix; print(msg_timestamp_prefix())" 2>>"$LOG_FILE")
  if [ -n "$msg_ts" ]; then
    content="$msg_ts
$content"
  fi

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
  local SYS_PROMPT_FILE="${ANSWER_FILE}.system_prompt"
  printf '%s' "$sys_prompt" > "$SYS_PROMPT_FILE"
  # Lock ownership token: every write to the lock file carries it, and every
  # destructive operation (overwrite at spawn, cleanup rm) first verifies the
  # token is still ours. Without ownership checks, a waiter that legitimately
  # reclaimed a stale lock could be silently dispossessed by the original
  # handler's blind writes (the 2026-06-11 review found three such races).
  # Lock file format: "<pid-or-'acquiring'> <token>" — readers that kill the
  # holder (restart.sh, /stop, staleness checks) take the FIRST field.
  local _lock_token
  _lock_token="$$.$(date +%s).$RANDOM"
  local waited=0
  local _busy_notice_sent=0
  while :; do
    # Acquire atomically (noclobber) BEFORE spawning Claude. Stale-lock
    # policy: a NUMERIC dead pid → reclaim immediately; an alive holder is
    # waited out up to 6200s (the 6000s watchdog ceiling governs in-flight
    # calls — a shorter cutoff here used to break the lock of legitimately
    # long-running handlers).
    until (set -C; printf 'acquiring %s' "$_lock_token" > "$LOCK_FILE") 2>/dev/null; do
      _lock_holder=$(awk '{print $1}' "$LOCK_FILE" 2>/dev/null)
      case "$_lock_holder" in
        ''|*[!0-9]*) : ;;  # placeholder — treat as alive (just acquired)
        *)
          if ! kill -0 "$_lock_holder" 2>/dev/null; then
            log_warn "[$session_id] Lock holder PID $_lock_holder is dead — reclaiming stale lock"
            rm -f "$LOCK_FILE"
            waited=0
            continue
          fi ;;
      esac
      if [ "$waited" -ge 6200 ]; then
        log_warn "[$session_id] Lock wait exceeded watchdog ceiling (${waited}s) — force-clearing"
        rm -f "$LOCK_FILE"
        waited=0
        continue
      fi
      if [ "$((waited % 30))" -eq 0 ] && [ "$waited" -gt 0 ]; then
        log_info "[$session_id] Session busy, waiting... (${waited}s)"
        if [ "$_busy_notice_sent" -eq 0 ]; then
          _busy_notice_sent=1
          lark_reply_text "$message_id" "前一条还在处理，我已把这条排队；轮到它时会继续，不需要重发。" >/dev/null 2>&1 || true
        fi
      fi
      sleep 5
      waited=$((waited + 5))
    done
    # Re-resolve after acquiring: the conversation may have rotated to a new
    # session while we waited (background-job auto-promotion does this). Our
    # session_id was resolved at dispatch time — resuming it now would write
    # into the transcript the promoted Claude is still appending to.
    _cur_sid=$(JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" python3 -c "
import json, os
try:
    print(json.load(open(os.environ['JV_TRACKER'])).get(os.environ['JV_KEY'], {}).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
    if [ -n "$_cur_sid" ] && [ "$_cur_sid" != "$session_id" ]; then
      log_info "[$session_id] Conversation rotated to $_cur_sid while waiting — switching"
      rm -f "$LOCK_FILE"
      session_id="$_cur_sid"
      LOCK_FILE="$JARVIS_DIR/.session_lock_${session_id}"
      continue
    fi
    break
  done

  # Run Claude with automatic retry on empty response
  local session_file="$CLAUDE_PROJECT_DIR/${session_id}.jsonl"
  local _claude_pid
  local answer=""
  local _attempt
  local _watchdog_killed=0  # set to 1 only when the 6000s watchdog did the kill
  local _use_claude_backup=0
  local _claude_backup_tried=0
  local _openai_tried=0
  local _answer_provider=""
  local _answer_model=""
  # REQ-77: the model to use this attempt. Degrades (opus→sonnet→haiku) if a
  # spawn fails with a model-unavailable / spend-limit stderr, instead of
  # looping to empty death ("Continue / No response requested").
  local _cur_model="$MAIN_MODEL"

  for _attempt in 1 2 3 4; do
    if [ "$_attempt" -gt 1 ]; then
      log_info "[$session_id] Retry attempt $_attempt after empty response (sleeping 3s)"
      sleep 3
    fi

    # Ownership check before every spawn: if the lock no longer carries our
    # token (a waiter reclaimed it as stale, or promotion released it and
    # someone else acquired), we must NOT touch this session again.
    if ! grep -q "$_lock_token" "$LOCK_FILE" 2>/dev/null; then
      log_warn "[$session_id] Lost lock ownership before attempt $_attempt — aborting retries"
      answer=""
      break
    fi

    if [ -f "$session_file" ]; then
      [ "$_attempt" -eq 1 ] && log_info "[$session_id] Resuming session"
      if [ "$_use_claude_backup" -eq 1 ]; then
        log_warn "[$session_id] Calling Claude Code backup provider model=$_cur_model"
        (cd "$WORK_DIR" && printf '%s' "$content" | env \
          ANTHROPIC_AUTH_TOKEN="$CLAUDE_BACKUP_AUTH_TOKEN" \
          ANTHROPIC_BASE_URL="$CLAUDE_BACKUP_BASE_URL" \
          claude -p \
          --resume "$session_id" \
          --model "$_cur_model" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          --output-format json \
          2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
      else
        log_info "[$session_id] Calling primary Claude Code model=$_cur_model"
        (cd "$WORK_DIR" && printf '%s' "$content" | claude -p \
          --resume "$session_id" \
          --model "$_cur_model" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          --output-format json \
          2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
      fi
    else
      [ "$_attempt" -eq 1 ] && log_info "[$session_id] New session"
      if [ "$_use_claude_backup" -eq 1 ]; then
        log_warn "[$session_id] Calling Claude Code backup provider model=$_cur_model"
        (cd "$WORK_DIR" && printf '%s' "$content" | env \
          ANTHROPIC_AUTH_TOKEN="$CLAUDE_BACKUP_AUTH_TOKEN" \
          ANTHROPIC_BASE_URL="$CLAUDE_BACKUP_BASE_URL" \
          claude -p \
          --session-id "$session_id" \
          --model "$_cur_model" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          --output-format json \
          2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
      else
        log_info "[$session_id] Calling primary Claude Code model=$_cur_model"
        (cd "$WORK_DIR" && printf '%s' "$content" | claude -p \
          --session-id "$session_id" \
          --model "$_cur_model" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          --output-format json \
          2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
      fi
    fi
    _claude_pid=$!
    printf '%s %s' "$_claude_pid" "$_lock_token" > "$LOCK_FILE"
    # Live activity stream: poll session file every 20s, send new tool calls to user
    # Also acts as watchdog: kills Claude after 6000s
    (_session_jsonl="$CLAUDE_PROJECT_DIR/${session_id}.jsonl"
     # Snapshot current tool count so we only report NEW tools from this call
     _last_tool_count=$(python3 -c "
import json
n=0
try:
    with open('$CLAUDE_PROJECT_DIR/${session_id}.jsonl') as f:
        for line in f:
            for b in (json.loads(line).get('message',{}).get('content',[]) or []):
                if isinstance(b,dict) and b.get('type')=='tool_use': n+=1
except: pass
print(n)
" 2>/dev/null || echo 0)
     _elapsed=0
     # Responsiveness policy is single-sourced + tested in core/responsiveness
     # (REQ-59). Pull the tuned constants here; fall back to literals if the
     # module call ever fails so the loop never breaks.
     eval "$(python3 -m core.responsiveness env 2>/dev/null)"
     : "${JV_POLL_FIRST:=6}" "${JV_POLL_STEADY:=20}"
     : "${JV_THINKING_ACK:=💭 收到了，正在想……（稍等）}"
     # First poll fast (~6s) so the user sees a sign of life quickly, then
     # settle to 20s to avoid spam. The instant "Typing" reaction already
     # fired at dispatch; this loop adds the FIRST textual feedback within
     # ~6s — either a tool narration (🔧) or, when opus is just thinking with
     # no tool calls, a one-time "received, thinking" note so the long
     # generation (median ~100s on opus) isn't dead silence.
     _poll="$JV_POLL_FIRST"
     _thinking_sent=0
     while [ "$_elapsed" -lt 6000 ]; do
       sleep "$_poll"
       _elapsed=$((_elapsed + _poll))
       _poll="$JV_POLL_STEADY"
       if ! kill -0 $_claude_pid 2>/dev/null; then break; fi
       # Extract tool call descriptions, compare with last snapshot
       _new_tools=$(python3 -c "
import json, sys
descs = []
try:
    with open('$_session_jsonl') as f:
        for line in f:
            obj = json.loads(line)
            for block in (obj.get('message',{}).get('content',[]) or []):
                if isinstance(block,dict) and block.get('type')=='tool_use':
                    inp = block.get('input',{})
                    d = inp.get('description','')
                    if not d:
                        name = block.get('name','')
                        path = inp.get('file_path','')
                        cmd = inp.get('command','')[:50]
                        pattern = inp.get('pattern','')[:30]
                        if path: d = f'{name}: {path.split(\"/\")[-1]}'
                        elif cmd: d = f'{name}: {cmd}'
                        elif pattern: d = f'{name}: {pattern}'
                        else: d = name
                    descs.append(d[:60])
except: pass
# Output: total_count then new descriptions (after offset)
offset = int(sys.argv[1]) if len(sys.argv)>1 else 0
print(len(descs))
for d in descs[offset:]:
    print(d)
" "$_last_tool_count" 2>/dev/null)
       _new_count=$(echo "$_new_tools" | head -1)
       _new_descs=$(echo "$_new_tools" | tail -n +2)
       if [ -n "$_new_descs" ] && [ "$_new_count" -gt "$_last_tool_count" ] 2>/dev/null; then
         _thinking_sent=1   # tool narration IS the sign of life — suppress the thinking note
         _formatted=$(echo "$_new_descs" | while IFS= read -r _d; do
           [ -n "$_d" ] && echo "• $_d"
         done)
         if [ -n "$_formatted" ]; then
           # REQ-18 (user-designed 5/29): narrate progress with a fast cheap
           # model instead of dumping raw tool names — "它搜了什么网站、有没有
           # 真的去看，还是在幻觉，对用户是非常重要的信息". Throttled to ≥60s
           # between narrations; raw list is the fallback on any failure.
           _now_s=$(date +%s)
           if [ $((_now_s - ${_last_narrate:-0})) -ge 60 ]; then
             _last_narrate=$_now_s
             # Narrate in a BACKGROUND fork: an inline call (even with a real
             # timeout) blocks this poll loop, delaying the 120s promotion
             # check and the 6000s watchdog by up to 12s per narration.
             ( _n=$(with_timeout 12 claude -p \
                 "下面是 AI 助手正在执行的工具调用列表。用一句中文（≤40字）向用户转述它正在做什么、信息来自哪里。只输出那一句话，不要前缀。
$_formatted" \
                 --model haiku --no-session-persistence --disable-slash-commands \
                 --dangerously-skip-permissions </dev/null 2>/dev/null | head -2 | tr '\n' ' ')
               if [ -n "$_n" ] && [ "${#_n}" -lt 200 ]; then
                 lark_reply_text "$message_id" "🔧 $_n" >/dev/null 2>&1
               else
                 lark_reply_text "$message_id" "🔧 $_formatted" >/dev/null 2>&1
               fi ) >/dev/null 2>&1 &
           else
             lark_reply_text "$message_id" "🔧 $_formatted" >/dev/null 2>&1 || true
           fi
         fi
         _last_tool_count="$_new_count"
       elif [ "$_thinking_sent" -eq 0 ]; then
         # No tool calls yet and the reply isn't back — opus is "just
         # thinking". Send ONE textual ack (the reaction alone left a long
         # silent gap). Fast replies (<6s) never reach here: the kill -0
         # check above broke the loop. Fires at most once per turn.
         _thinking_sent=1
         lark_reply_text "$message_id" "$JV_THINKING_ACK" >/dev/null 2>&1 || true
       fi
       # ── Auto-promotion (REQ-16 MVP-2): a call running >120s becomes a
       # background job. Release the conversation instead of blocking it —
       # "一跑跑3个小时我就用不了这个机器人" was the single harshest complaint
       # in the interaction audit. The conversation rotates to a fresh session
       # so new messages never resume the transcript this Claude still writes;
       # the result comes back via the normal reply + pending_merge.
       if [ "$_elapsed" -ge 120 ] && [ ! -f "${ANSWER_FILE}.promoted" ] && kill -0 $_claude_pid 2>/dev/null; then
         _bg_job_id=$(JV_JOBS_DIR="$JOBS_DIR" JV_CONV_KEY="$conv_key" \
           JV_DESC="auto-promoted: ${content:0:120}" JV_MSG_ID="$message_id" \
           python3 "$JARVIS_DIR/core/jobs.py" create 2>>"$LOG_FILE")
         if [ -n "$_bg_job_id" ]; then
           JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" set-pid "$_bg_job_id" "$_claude_pid" \
             2>>"$LOG_FILE" || true
           printf '%s' "$_bg_job_id" > "${ANSWER_FILE}.promoted"
           if JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" JV_SDIR="$CLAUDE_PROJECT_DIR" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.session import SessionManager
sm = SessionManager(os.environ['JV_TRACKER'], os.environ['JV_SDIR'])
sm.force_rotate(os.environ['JV_KEY'])
" 2>>"$LOG_FILE"; then
             # Release only OUR lock (ownership may already have moved)
             if grep -q "$_lock_token" "$LOCK_FILE" 2>/dev/null; then
               rm -f "$LOCK_FILE"
             fi
             lark_reply_text "$message_id" "⏳ 这个任务跑得比较久，已自动转后台（job \`$_bg_job_id\`）。会话已释放，可以继续找我聊别的；做完我会把结果发回来。发 jobs 可查进度，发 cancel $_bg_job_id 可取消。" >/dev/null 2>&1 || true
             log_info "[$session_id] Promoted to background job $_bg_job_id after ${_elapsed}s — lock released"
           else
             # Rotation failed: keep the lock (conversation stays busy) so a
             # follow-up message can't resume the transcript mid-write.
             rm -f "${ANSWER_FILE}.promoted"
             log_warn "[$session_id] Promotion rotate failed — keeping lock, job $_bg_job_id orphaned to sweeper"
           fi
         fi
       fi
     done
     # Watchdog timeout. Drop a marker so the parent can tell a genuine 6000s
     # timeout apart from a SIGTERM that came from a restart / external kill —
     # both surface as exit 143, but only the real timeout should tell the user
     # to resume with 「继续」.
     if kill -0 $_claude_pid 2>/dev/null; then
       : > "${ANSWER_FILE}.watchdog"
       kill $_claude_pid 2>/dev/null
       log_warn "[$session_id] Claude killed by 6000s watchdog"
     fi
    ) &
    _watchdog_pid=$!
    wait $_claude_pid 2>/dev/null
    local _exit_code=$?
    kill $_watchdog_pid 2>/dev/null 2>&1; wait $_watchdog_pid 2>/dev/null
    # Reset the lock to placeholder while we own it: between attempts the
    # file would otherwise hold a DEAD claude pid, which a waiter's staleness
    # check reads as "handler crashed" and reclaims mid-retry.
    if grep -q "$_lock_token" "$LOCK_FILE" 2>/dev/null; then
      printf 'acquiring %s' "$_lock_token" > "$LOCK_FILE"
    fi
    # Did the 6000s watchdog do this kill, or was it a restart / external SIGTERM?
    [ -f "${ANSWER_FILE}.watchdog" ] && _watchdog_killed=1

    # Extract the final assistant text from the --output-format json envelope:
    # one object {"result": "<final text>", "subtype": "success", ...}. Parsing
    # .result is immune to stdout pollution from a task-notification / sub-agent
    # resume dump — the root cause of the 124k-char leak on 2026-05-30 (a
    # background task completed mid-resume and its full envelope went to stdout,
    # which the old `cat` blasted at the user). On any parse failure or non-
    # success subtype we yield "" so the empty-answer retry path takes over.
    answer=$(JV_AF="$ANSWER_FILE" python3 -c "
import json, os, sys
try:
    obj = json.load(open(os.environ['JV_AF']))
    r = obj.get('result')
    if obj.get('subtype') == 'success' and isinstance(r, str):
        sys.stdout.write(r)
    elif isinstance(r, str):
        sys.stdout.write(r)  # surface error text → looks_like_error filters it
except Exception:
    pass
" 2>/dev/null)
    local _stderr_content
    _stderr_content=$(head -5 "${ANSWER_FILE}.stderr" 2>/dev/null | tr '\n' ' ')
    local _answer_is_error=0
    if [ -n "$answer" ] && looks_like_error "$answer"; then
      _answer_is_error=1
    fi
    local _model_error_text="${_stderr_content:-}"
    if [ "$_answer_is_error" -eq 1 ]; then
      _model_error_text="${_model_error_text} ${answer}"
    fi

    if [ -z "$answer" ] || [ "$_answer_is_error" -eq 1 ]; then
      if [ "$_answer_is_error" -eq 1 ]; then
        log_warn "[$session_id] Error-looking answer from Claude (attempt $_attempt, exit=$_exit_code, content=${answer:0:180})"
      else
        log_warn "[$session_id] Empty answer from Claude (attempt $_attempt, exit=$_exit_code, stderr=${_stderr_content:-none})"
      fi
      # Promoted to background: never retry — the conversation has moved on
      # (rotated session), so a silent re-run would race the new session's
      # traffic and double-bill a task the user already saw go background.
      if [ -f "${ANSWER_FILE}.promoted" ]; then
        break
      fi
      # exit 143 = SIGTERM: either the 6000s watchdog (task ran long) or a
      # restart/external kill. Either way, retrying in-loop is pointless (the
      # process is already gone), so stop now. The user-facing message below
      # branches on whether the watchdog marker is present.
      if [ "${_exit_code:-0}" -eq 143 ]; then
        break
      fi
      # REQ-77: if the empty answer was a MODEL error (unavailable / banned /
      # spend limit) rather than a transient blip, degrade the model for the
      # next attempt instead of retrying the same broken model to death.
      _fallback=$(printf '%s' "$_model_error_text" | python3 -m core.model_fallback "$_cur_model" 2>/dev/null)
      if [ -n "$_fallback" ]; then
        log_warn "[$session_id] Model error on $_cur_model → degrading to $_fallback (REQ-77)"
        _cur_model="$_fallback"
      elif [ "$_use_claude_backup" -eq 0 ] \
        && [ "${CLAUDE_BACKUP_ENABLED:-true}" = "true" ] \
        && [ "$_claude_backup_tried" -eq 0 ] \
        && [ -n "${CLAUDE_BACKUP_AUTH_TOKEN:-}" ] \
        && [ -n "${CLAUDE_BACKUP_BASE_URL:-}" ] \
        && printf '%s' "$_model_error_text" | python3 -m core.model_fallback --is-model-error 2>/dev/null; then
        _claude_backup_tried=1
        _use_claude_backup=1
        _cur_model="$MAIN_MODEL"
        log_warn "[$session_id] Primary Claude exhausted on $_cur_model → trying Claude Code backup provider"
      elif [ "${OPENAI_FALLBACK_ENABLED:-true}" = "true" ] \
        && [ -n "${OPENAI_API_KEY:-}" ] \
        && [ "$_openai_tried" -eq 0 ] \
        && printf '%s' "$_model_error_text" | python3 -m core.model_fallback --is-model-error 2>/dev/null; then
        _openai_tried=1
        log_warn "[$session_id] Claude model chain exhausted on $_cur_model → trying OpenAI fallback (${OPENAI_FALLBACK_MODEL:-gpt-5.2})"
        answer=$(printf '%s' "$content" | JV_SYSTEM_PROMPT_FILE="$SYS_PROMPT_FILE" \
          python3 -m core.openai_fallback \
          2>"${ANSWER_FILE}.openai.stderr")
        _openai_exit=$?
        if [ "$_openai_exit" -eq 0 ] && [ -n "$answer" ]; then
          _answer_provider="GPT fallback"
          _answer_model="${OPENAI_FALLBACK_MODEL:-gpt-5.2}"
          log_warn "[$session_id] OpenAI fallback succeeded (${#answer} chars)"
          break
        fi
        _openai_err=$(head -5 "${ANSWER_FILE}.openai.stderr" 2>/dev/null | tr '\n' ' ')
        log_warn "[$session_id] OpenAI fallback failed (exit=$_openai_exit, stderr=${_openai_err:-none})"
      fi
      # On first failure, session file may have been created — update for retry
      session_file="$CLAUDE_PROJECT_DIR/${session_id}.jsonl"
    else
      if [ "$_use_claude_backup" -eq 1 ]; then
        _answer_provider="Claude backup"
      else
        _answer_provider="Claude primary"
      fi
      _answer_model="$_cur_model"
      break
    fi
  done

  local _promoted_job=""
  [ -f "${ANSWER_FILE}.promoted" ] && _promoted_job=$(cat "${ANSWER_FILE}.promoted" 2>/dev/null)
  rm -f "$ANSWER_FILE" "${ANSWER_FILE}.stderr" "${ANSWER_FILE}.watchdog" \
    "${ANSWER_FILE}.promoted" "${ANSWER_FILE}.openai.stderr" "$SYS_PROMPT_FILE"
  # Remove the lock ONLY if we still own it: after promotion released it (or
  # a staleness reclaim), it may belong to another live handler — an
  # unconditional rm here silently unlocked their in-flight session.
  if grep -q "$_lock_token" "$LOCK_FILE" 2>/dev/null; then
    rm -f "$LOCK_FILE"
  fi

  # Filter error-like answers — never send them to the user as the "real" reply
  local reply=""
  if [ -n "$answer" ] && ! looks_like_error "$answer"; then
    reply="$answer"
  fi

  # Promoted-job bookkeeping (REQ-16 MVP-2): close the registry entry and, on
  # success, queue the result for merge into the conversation's NEW session
  # (it rotated away at promotion time and hasn't seen this answer).
  if [ -n "$_promoted_job" ]; then
    if [ -n "$reply" ]; then
      JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" finish "$_promoted_job" completed \
        2>>"$LOG_FILE" || true
      jq -cn --arg key "$conv_key" --arg job "$_promoted_job" \
        --arg ts "$(date '+%Y-%m-%d %H:%M')" --arg summary "${reply:0:1500}" \
        '{conv_key:$key,job_id:$job,ts:$ts,summary:$summary}' \
        >> "$JOBS_DIR/pending_merge.jsonl" 2>>"$LOG_FILE" || true
      log_info "[$session_id] Promoted job $_promoted_job completed (${#reply} chars)"
    else
      JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" finish "$_promoted_job" failed \
        2>>"$LOG_FILE" || true
      log_warn "[$session_id] Promoted job $_promoted_job finished empty/error"
    fi
  fi

  if [ -z "$reply" ]; then
    log_warn "[$session_id] Final empty/error answer from Claude (${#answer} chars after ${_attempt:-?} attempts)"
    if [ -n "$answer" ]; then
      log_warn "[$session_id] Suppressed content: ${answer:0:500}"
    fi
    [ -n "$reaction_id" ] && lark_remove_reaction "$message_id" "$reaction_id"
    # Tell user exactly what happened — not a vague "try again"
    if [ "${#answer}" -eq 0 ]; then
      if [ "${_exit_code:-0}" -eq 143 ] && [ "$_watchdog_killed" -eq 1 ] && [ -n "$_promoted_job" ]; then
        # Promoted job hit the 6000s ceiling. 「继续」 would land in the NEW
        # (rotated) session and not resume this work — say so honestly.
        lark_reply_text "$message_id" \
          "后台任务 \`$_promoted_job\` 运行超过看门狗上限（100 分钟）被中断。它的中间产出已存在原 session 里；要接着做的话告诉我任务内容，我重新起一个。" >/dev/null
      elif [ "${_exit_code:-0}" -eq 143 ] && [ "$_watchdog_killed" -eq 1 ]; then
        # Genuine 6000s watchdog timeout: the task really ran long. Resuming
        # with 「继续」 is the right recovery.
        lark_reply_text "$message_id" \
          "任务运行超过看门狗上限被中断（exit 143，不是 API 问题）。进度已存入 session，直接说「继续」即可接着干。" >/dev/null
      elif [ "${_exit_code:-0}" -eq 143 ]; then
        # 143 WITHOUT the watchdog marker = a restart / external SIGTERM killed
        # the in-flight Claude. Telling the user to say 「继续」 here is exactly
        # the bug that produced the restart-loop nag: 「继续」 re-runs whatever
        # was interrupted (often the very restart). Stay silent — the post-restart
        # startup path already notifies "重启中断了，请重发" from the message queue.
        log_warn "[$session_id] exit=143 without watchdog marker — restart/external kill, staying silent"
      else
        # Transient empty response (API blip). We already retried silently up to
        # 4x with backoff above. Nagging "请稍后重试" just forces the user to tell
        # us to retry by hand — exactly the boring loop they asked us to remove.
        # Stay silent: the reaction is cleared above so the turn visibly ends,
        # and the user can resend if they were actually waiting on a reply.
        log_warn "[$session_id] Empty after $_attempt attempts — staying silent (user opted out of the retry nag)"
      fi
    else
      lark_reply_text "$message_id" \
        "Claude 的回复被安全过滤器拦截了（可能包含错误信息）。请换个方式重试。" >/dev/null
    fi
    return
  fi

  local _model_footer=""
  if [ -n "$_answer_provider" ] && [ -n "$_answer_model" ]; then
    _model_footer="Model: ${_answer_provider} ${_answer_model}"
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

  if [ -n "$_model_footer" ]; then
    reply="${reply}

${_model_footer}"
  fi

  log_info "[$session_id] Replied (${#reply} chars)"

  # ── Oversized-reply guard ──
  # A normal reply is at most a few thousand chars. Anything far larger means
  # something leaked into stdout (e.g. a sub-agent's full report after a
  # task-notification resume) — never blast that at the user. Cap, note, warn.
  REPLY_MAX_CHARS=${REPLY_MAX_CHARS:-6000}
  if [ "${#reply}" -gt "$REPLY_MAX_CHARS" ]; then
    log_warn "[$session_id] Oversized reply (${#reply} chars) capped to $REPLY_MAX_CHARS — head: ${reply:0:200}"
    reply="${reply:0:$REPLY_MAX_CHARS}

…（回复过长已截断，可能是后台任务输出泄漏，已记录日志）"
  fi

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
  # claude CLI requires --session-id to be a valid UUID; the old "bg-<jobid>"
  # scheme is rejected ("Invalid session ID. Must be a valid UUID.").
  local bg_session_id
  bg_session_id="$(uuidgen 2>/dev/null | tr 'A-Z' 'a-z')"
  [ -z "$bg_session_id" ] && bg_session_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
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

  # Inherit conversation context (REQ-16 MVP-1): fork from the conversation's
  # active session if it has a transcript — the job sees the full dialog
  # history without polluting the main session, and reuses the prompt cache.
  # Falls back to a fresh session for conversations with no history.
  local _main_sid=""
  _main_sid=$(JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" python3 -c "
import json, os
try:
    print(json.load(open(os.environ['JV_TRACKER'])).get(os.environ['JV_KEY'], {}).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

  # set -m: give the job its OWN process group (REQ-38). Without it the
  # subshell shares bot.sh's group and cancel_job's killpg SIGTERMed the
  # ENTIRE bot — user-facing "cancel <job>" restarted the whole product.
  # With its own group, killpg cleanly reaps subshell + with_timeout + claude.
  set -m
  if [ -n "$_main_sid" ] && [ -f "$CLAUDE_PROJECT_DIR/${_main_sid}.jsonl" ]; then
    log_info "[bg:$job_id] Forking from session $_main_sid"
    (cd "$WORK_DIR" && with_timeout 6000 claude -p "$content" \
      --resume "$_main_sid" --fork-session \
      --model "$MAIN_MODEL" \
      --append-system-prompt "$sys_prompt" \
      --dangerously-skip-permissions \
      < /dev/null 2>>"$log_file_job" > "$output_file" || true) &
  else
    (cd "$WORK_DIR" && with_timeout 6000 claude -p "$content" \
      --session-id "$bg_session_id" \
      --model "$MAIN_MODEL" \
      --append-system-prompt "$sys_prompt" \
      --dangerously-skip-permissions \
      < /dev/null 2>>"$log_file_job" > "$output_file" || true) &
  fi
  local _bg_pid=$!
  set +m

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

  # Queue the result for context merge (REQ-16): the conversation's next
  # message gets this summary prepended, so the dialog "knows" what the job
  # found instead of the result living only in a notification card.
  if [ "$status" = "completed" ]; then
    jq -cn --arg key "$conv_key" --arg job "$job_id" \
      --arg ts "$(date '+%Y-%m-%d %H:%M')" --arg summary "${output:0:1500}" \
      '{conv_key:$key,job_id:$job,ts:$ts,summary:$summary}' \
      >> "$JOBS_DIR/pending_merge.jsonl" 2>>"$LOG_FILE" || true
  fi

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
  # Save in-flight message sessions so next startup can notify user
  _queue_file="$JARVIS_DIR/.message_queue"
  rm -f "$_queue_file"
  for _lock in "$JARVIS_DIR"/.session_lock_*; do
    [ -f "$_lock" ] || continue
    basename "$_lock" | sed 's/^\.session_lock_//' >> "$_queue_file"
  done
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
  local _fails=0 _last_fail=0 _ticks=0
  local _stream_fails=0 _stream_last_fail=0 _admin_fails=0 _admin_last_fail=0
  while true; do
    # Hourly housekeeping (120 ticks × 30s). Startup-only rotation isn't
    # enough: a bot that stays up for weeks grows jarvis.log and tmp/ without
    # bound (tmp/ held 5.5MB of stale media downloads with no cleanup at all).
    _ticks=$((_ticks + 1))
    if [ $((_ticks % 120)) -eq 0 ]; then
      if [ -f "$LOG_FILE" ] && [ "$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
        # Archive before truncating: tail-only rotation made WARN-rate audits
        # impossible (history was simply destroyed). Keep 3 generations.
        mv -f "$LOG_FILE.2" "$LOG_FILE.3" 2>/dev/null || true
        mv -f "$LOG_FILE.1" "$LOG_FILE.2" 2>/dev/null || true
        cp -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
        # copytruncate, not tail+mv: the python children hold O_APPEND fds
        # from `2>>` redirects — a rename would silently divert their logs
        # to the replaced inode until the next restart.
        tail -500 "$LOG_FILE" > "$LOG_FILE.tmp" \
          && cat "$LOG_FILE.tmp" > "$LOG_FILE" \
          && rm -f "$LOG_FILE.tmp"
        log_info "[watchdog] Rotated jarvis.log (archived to jarvis.log.1)"
      fi
      find "$JARVIS_DIR/tmp" -type f -mtime +7 -delete 2>/dev/null || true
    fi
    # Deploy guard (red-team fix): during a restart.sh window the stack is
    # being torn down on purpose. If the watchdog relaunches heartbeat/stream/
    # admin here, restart.sh's kill_bot races it and can ORPHAN the relaunched
    # child (it's no longer in the pidfile the parent's cleanup trap knows).
    # Skip ALL relaunches while .deploying is fresh. (The heartbeat_loop
    # singleton flock is the second line of defense.)
    if [ -f "$JARVIS_DIR/.deploying" ]; then
      sleep 30
      continue
    fi
    if ! kill -0 "$HEARTBEAT_PID" 2>/dev/null; then
      local _now
      _now=$(date +%s)
      # Reset the failure counter once the heartbeat has stayed up for 10min,
      # so isolated crashes don't accumulate toward the breaker forever.
      if [ $((_now - _last_fail)) -gt 600 ]; then _fails=0; fi
      _fails=$((_fails + 1))
      _last_fail=$_now
      # Circuit breaker: several crashes inside a short window almost always
      # mean a systemic fault (syntax error, OOM, bad config). Hot-restarting
      # every 30s just spams the log and burns CPU — back off hard instead.
      if [ "$_fails" -ge 4 ]; then
        log_warn "[watchdog] Heartbeat crashed ${_fails}x within 10min — backing off 300s before retry"
        sleep 300
      fi
      log_warn "[watchdog] Heartbeat PID $HEARTBEAT_PID died — restarting (fail #${_fails})"
      # Heartbeat is a Python module now (it used to be a bash function named
      # heartbeat_loop — calling that here was a no-op that never restarted it).
      # Relaunch exactly like the initial launch above, inheriting exported env.
      python3 -m core.heartbeat_loop 2>>"$LOG_FILE" &
      HEARTBEAT_PID=$!
      log_info "[watchdog] Heartbeat restarted (PID: $HEARTBEAT_PID)"
    fi
    # Supervision dead zones closed (REQ-40): ef_stream_loop and admin.py
    # previously had NO watchdog — their death was invisible until the next
    # full restart (EigenFlux PMs silently stopped; Lark RichView links 404'd).
    # Same 4-crashes-in-10min breaker discipline, separate counters.
    if [ -n "${STREAM_PID:-}" ] && ! kill -0 "$STREAM_PID" 2>/dev/null; then
      _now=$(date +%s)
      if [ $((_now - _stream_last_fail)) -gt 600 ]; then _stream_fails=0; fi
      _stream_fails=$((_stream_fails + 1)); _stream_last_fail=$_now
      # Mirror the heartbeat branch (red-team fix): restart UNCONDITIONALLY,
      # only the backoff sleep is gated on the breaker. The old `elif -eq 4`
      # give-up never recovered — fails kept climbing past 4 (matching no
      # branch) because the process was never restarted, so the >600s reset
      # was unreachable and EF PMs stayed dead forever.
      if [ "$_stream_fails" -ge 4 ]; then
        log_warn "[watchdog] EF stream crashed ${_stream_fails}x within 10min — backing off 300s before retry"
        sleep 300
      fi
      log_warn "[watchdog] EF stream PID $STREAM_PID died — restarting (fail #${_stream_fails})"
      EIGENFLUX_HOST="${EIGENFLUX_HOST:-jarvis}" EIGENFLUX_CHANNEL="${EIGENFLUX_CHANNEL:-lark}" \
        LOG_FILE="$LOG_FILE" python3 -m core.ef_stream_loop 2>>"$LOG_FILE" &
      STREAM_PID=$!
      log_info "[watchdog] EF stream restarted (PID: $STREAM_PID)"
    fi
    if [ "$ADMIN_ENABLED" = "true" ] && [ -n "${ADMIN_PID:-}" ] && ! kill -0 "$ADMIN_PID" 2>/dev/null; then
      _now=$(date +%s)
      if [ $((_now - _admin_last_fail)) -gt 600 ]; then _admin_fails=0; fi
      _admin_fails=$((_admin_fails + 1)); _admin_last_fail=$_now
      if [ "$_admin_fails" -ge 4 ]; then
        log_warn "[watchdog] Admin crashed ${_admin_fails}x within 10min — backing off 300s before retry (likely port conflict)"
        sleep 300
      fi
      log_warn "[watchdog] Admin PID $ADMIN_PID died — restarting (fail #${_admin_fails})"
      python3 "$JARVIS_DIR/admin.py" >>"$LOG_FILE" 2>&1 &
      ADMIN_PID=$!
      log_info "[watchdog] Admin restarted (PID: $ADMIN_PID)"
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

run_lark_listener_once() {
  lark_subscribe_messages \
    | while IFS= read -r line; do
      # Skip SDK error lines (they shouldn't appear on stdout but just in case)
      case "$line" in "[SDK Error]"*) continue ;; esac

      # ── Read receipts & reactions (REQ-15 engagement attribution) ────
      # read = user saw the message; reaction = lightweight engagement.
      # Recorded to engagement_log so analysis can tell "read but ignored"
      # from "never seen" instead of guessing from reply timing.
      _etype=$(echo "$line" | jq -r '.header.event_type // .event_type // empty' 2>/dev/null)
      case "$_etype" in
        im.message.message_read_v1)
          _read_ids=$(echo "$line" | jq -c '.event.message_id_list // []' 2>/dev/null)
          jq -cn --arg ts "$(date '+%Y-%m-%d %H:%M')" \
            --argjson ids "${_read_ids:-[]}" --argjson epoch "$(date +%s)" \
            '{ts:$ts,type:"read",message_ids:$ids,epoch:$epoch}' \
            >> "$JARVIS_DIR/engagement_log.jsonl" 2>/dev/null || true
          continue ;;
        im.message.reaction.created_v1)
          # Ignore the bot's own reactions (e.g. the "Typing" indicator)
          _re_op=$(echo "$line" | jq -r '.event.operator_type // empty' 2>/dev/null)
          if [ "$_re_op" != "app" ]; then
            _re_mid=$(echo "$line" | jq -r '.event.message_id // empty' 2>/dev/null)
            _re_emoji=$(echo "$line" | jq -r '.event.reaction_type.emoji_type // empty' 2>/dev/null)
            jq -cn --arg ts "$(date '+%Y-%m-%d %H:%M')" --arg mid "$_re_mid" \
              --arg emoji "$_re_emoji" --argjson epoch "$(date +%s)" \
              '{ts:$ts,type:"reaction",message_id:$mid,emoji:$emoji,epoch:$epoch}' \
              >> "$JARVIS_DIR/engagement_log.jsonl" 2>/dev/null || true
            log_info "[engagement] reaction $_re_emoji on ${_re_mid:0:20}"
            # ── One-tap watch-later (一键收藏, asked 5/06): reacting with ANY
            # emoji on one of OUR url-bearing messages saves it. This is the
            # card-button replacement that needs NO callback config — reaction
            # events already flow over this connection. Backgrounded: mget is
            # a network call and the event loop must not block.
            # Extraction lives in core/reaction_save.py — testable against
            # REAL captured mget shapes (the inline version shipped dead:
            # imagined fixtures didn't match lark-cli's pre-decoded output).
            ( _wl_info=$(lark-cli im +messages-mget --message-ids "$_re_mid" --as bot 2>>"$LOG_FILE" \
                | python3 -m core.reaction_save 2>>"$LOG_FILE")
              if [ -n "$_wl_info" ]; then
                _wl_saved=0
                _wl_dupes=0
                _wl_count=$(echo "$_wl_info" | jq -r '.items | length')
                _wl_i=0
                while [ "$_wl_i" -lt "$_wl_count" ]; do
                  _wl_t=$(echo "$_wl_info" | jq -r ".items[$_wl_i].title")
                  _wl_u=$(echo "$_wl_info" | jq -r ".items[$_wl_i].url")
                  _wl_out=$(python3 "$JARVIS_DIR/tasks/watchlater_save.py" "$_wl_t" "$_wl_u" "reaction" 2>>"$LOG_FILE")
                  case "$_wl_out" in
                    *已在*) _wl_dupes=$((_wl_dupes + 1)) ;;
                    *) _wl_saved=$((_wl_saved + 1)) ;;
                  esac
                  _wl_i=$((_wl_i + 1))
                done
                _wl_title=$(echo "$_wl_info" | jq -r .title)
                if [ "$_wl_saved" -gt 0 ]; then
                  if [ "$_wl_count" -gt 1 ]; then
                    lark_reply_text "$_re_mid" "✅ 已收藏 ${_wl_saved} 条链接（「${_wl_title:0:40}」等），空闲时段会提醒你。" >/dev/null 2>&1 || true
                  else
                    lark_reply_text "$_re_mid" "✅ 已收藏「${_wl_title:0:40}」，空闲时段会提醒你。（对带链接的消息点任意表情都会收藏）" >/dev/null 2>&1 || true
                  fi
                  log_info "[watchlater] Saved via reaction: ${_wl_saved} item(s), ${_wl_title:0:50}"
                elif [ "$_wl_dupes" -gt 0 ]; then
                  # Already saved (repeat reaction) — stay silent, no confirm
                  # spam, which also breaks any react-on-confirmation loop.
                  log_info "[watchlater] Reaction on already-saved content (${_wl_dupes} dupes) — silent"
                fi
              fi
            ) >/dev/null 2>&1 &
          fi
          continue ;;
        im.message.reaction.deleted_v1)
          continue ;;
      esac

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
          # source/rating come from the Lark card payload — build the JSON
          # with jq so embedded quotes can't produce a corrupt line.
          jq -cn --arg ts "$_fb_ts" --arg source "$_fb_source" --arg rating "$_fb_rating" \
            --argjson epoch "$(date +%s)" \
            '{ts:$ts,source:$source,type:"feedback",rating:$rating,epoch:$epoch}' \
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

      # Restart trigger consumption moved to heartbeat_loop (REQ-42): a
      # single consumer that polls every ~10s and spawns restart.sh detached.
      # The consumer that lived here only ran when a Lark event happened to
      # arrive — admin-clicked restarts raced between the two consumers and
      # the common winner gave 1-15 minutes of downtime.

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
      _parent_id=$(echo "$line" | jq -r '.parent_id // .event.message.parent_id // .event.message.upper_message_id // empty' 2>/dev/null)

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

      # ── Quote reply: fetch the quoted message and prepend as context ──
      if [ -n "$_parent_id" ] && [ "$_parent_id" != "null" ]; then
        _quoted_raw=$(lark-cli im +messages-mget --message-ids "$_parent_id" --as bot 2>>"$LOG_FILE")
        _quoted_text=$(echo "$_quoted_raw" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    msgs = d.get('data', {}).get('messages', d.get('messages', []))
    if not msgs: sys.exit(0)
    m = msgs[0]
    body = m.get('body', {}).get('content', m.get('content', ''))
    sender_name = m.get('_sender_name', '')
    # Parse inner content JSON
    try:
        inner = json.loads(body) if isinstance(body, str) else body
        if isinstance(inner, dict) and 'text' in inner:
            body = inner['text']
        elif isinstance(inner, dict) and 'content' in inner:
            parts = []
            for block in inner.get('content', []):
                for item in (block if isinstance(block, list) else [block]):
                    if isinstance(item, dict) and item.get('text'):
                        parts.append(item['text'])
            body = ' '.join(parts)
    except: pass
    body = str(body)[:500]
    prefix = f'[{sender_name}] ' if sender_name else ''
    print(f'{prefix}{body}')
except: pass
" 2>/dev/null)
        if [ -n "$_quoted_text" ]; then
          content="[Replying to: ${_quoted_text}]
${content}"
          log_info "Quote reply: parent=$_parent_id (${#_quoted_text} chars)"
        fi
        # ── Reply-to-intent matching (REQ-34B): if the quoted message is an
        # intention card, inject a structured hint so the main session closes
        # the loop deterministically instead of relying on LLM goodwill.
        # REQ-64: reply-based closure. The ledger maps the quoted card's
        # message_id → the closure root intents it carried (card_roots). If
        # this reply quotes such a card, try a DETERMINISTIC classify of the
        # reply (做了/没做/不用追) and close the loop directly via
        # record_closure(via=reply) — no dependence on the Feishu button
        # backend (0 closures ever happened via button/reply before this).
        # Only when the classifier is ambiguous do we fall back to the LLM hint.
        _intent_match=$(JV_PARENT="$_parent_id" JV_REPLY="$content" \
          JV_LEDGER="$JARVIS_DIR/data/.intent_card_ledger.jsonl" \
          JARVIS_DIR="$JARVIS_DIR" python3 -c "
import json, os, sys
sys.path.insert(0, os.environ['JARVIS_DIR'])
ledger = os.environ.get('JV_LEDGER', '')
parent = os.environ.get('JV_PARENT', '')
reply = os.environ.get('JV_REPLY', '')
roots, all_ids = [], []
try:
    for line in reversed(open(ledger, encoding='utf-8').read().splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if parent in (row.get('message_ids') or []):
            roots = row.get('card_roots') or []
            all_ids = row.get('intent_ids') or []
            break
except OSError:
    pass
if not (roots or all_ids):
    sys.exit(0)
# Deterministic closure — ONLY when the card carried exactly ONE closure-ask
# root (red-team fix: a multi-root card + one ambiguous reply would close BOTH
# loops with the same outcome — e.g. '约了' closing both dinner AND gym). With
# >1 root we can't tell which the reply answers → defer to the LLM hint.
from core.reply_closure import classify_reply, short_result
outcome = classify_reply(reply)
closed = []
if outcome and len(roots) == 1:
    from core.intentions import record_closure, get_intent
    try:
        # Only close a root that is actually AWAITING (red-team fix: a stale
        # ledger could name a 'none' root, and record_closure would fabricate
        # a closure on an intent that was never asking for one).
        row = get_intent(roots[0])
        if row and row.get('closure_status') == 'awaiting':
            if record_closure(roots[0], outcome=outcome, result=short_result(reply), via='reply'):
                closed.append(roots[0])
    except Exception:
        pass
# Output: 'CLOSED <ids>' if we closed deterministically, else 'HINT <ids>'
if closed:
    print('CLOSED ' + ','.join(closed))
elif all_ids:
    print('HINT ' + ','.join(all_ids))
" 2>>"$LOG_FILE")
        if [ "${_intent_match#CLOSED }" != "$_intent_match" ]; then
          _closed_ids="${_intent_match#CLOSED }"
          content="[闭环已自动记录:对意图 ${_closed_ids} 的回复已判定并 close,无需再调 intent_close] ${content}"
          log_info "Reply-based closure recorded (REQ-64): $_closed_ids"
        elif [ "${_intent_match#HINT }" != "$_intent_match" ]; then
          _hint_ids="${_intent_match#HINT }"
          content="[REPLY_TO_INTENT ids=${_hint_ids}] 这条回复是对意图卡片的回应。如果它回答了某个闭环问题，运行：python3 -m core.intentions close <对应id> done <他的一句话答复>（在 JARVIS_DIR 下），然后再正常回复。
${content}"
          log_info "Quote reply matched intent card (ambiguous→LLM hint): $_hint_ids"
        fi
      fi

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
              if [ -n "${OPENAI_API_KEY:-}" ] && command -v curl >/dev/null 2>&1; then
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
            # Truncate to prevent excessively large content from overwhelming Claude
            _forward_content="${_forward_content:0:5000}"
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

      # "发" — confirm pending EigenFlux broadcast; "不发" — cancel it
      if [ "$content" = "发" ] || [ "$content" = "不发" ]; then
        _pending_dir="$JARVIS_DIR/eigenflux/pending_publish"
        _latest_pending=$(ls -t "$_pending_dir"/*.json 2>/dev/null | head -1)
        if [ -z "$_latest_pending" ]; then
          lark_reply_text "$message_id" "没有待确认的广播。" >/dev/null
        elif [ "$content" = "发" ]; then
          _pub_data=$(cat "$_latest_pending")
          _pub_content=$(echo "$_pub_data" | jq -r '.content // empty')
          _pub_notes=$(echo "$_pub_data" | jq -c '.notes // {}')
          _pub_url=$(echo "$_pub_data" | jq -r '.url // empty')
          _pub_cmd=(eigenflux publish --content "$_pub_content" --notes "$_pub_notes" --accept-reply -f json)
          [ -n "$_pub_url" ] && _pub_cmd+=(--url "$_pub_url")
          if "${_pub_cmd[@]}" 2>>"$LOG_FILE"; then
            lark_reply_text "$message_id" "✅ 已广播" >/dev/null
            log_info "[eigenflux-publish] User confirmed, published: ${_pub_content:0:60}"
          else
            lark_reply_text "$message_id" "❌ 广播失败，请查看日志" >/dev/null
            log_warn "[eigenflux-publish] CLI failed on user-confirmed publish"
          fi
          rm -f "$_latest_pending"
        else
          rm -f "$_latest_pending"
          lark_reply_text "$message_id" "已取消广播。" >/dev/null
          log_info "[eigenflux-publish] User rejected pending broadcast"
        fi
        continue
      fi

      # "stop" / "cancel" — kill the running Claude process for this session (safety bypass).
      # Accept Chinese stop words too: Pascal naturally types「结束/停/停止/停下/取消」,
      # and previously those fell through to the LLM path — so a runaway/looping
      # session could never be halted by the user (the exact "我说结束它不理" bug).
      # Match the whole message only, so "取消那个日程" won't be treated as a stop.
      _content_trimmed=$(printf '%s' "$content" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
      case "$_content_trimmed" in
        结束|停|停止|停下|取消|停一下|别跑了) content_lower="stop" ;;
      esac
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
          # Lock format is "<pid> <token>" — take the first field
          _stop_pid=$(awk '{print $1}' "$_stop_lock" 2>/dev/null)
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
      # Acknowledge IMMEDIATELY with a "working on it" reaction, before any
      # synchronous work (engagement spawn, session compact) runs. The reaction
      # only needs message_id, which we already have, so the user sees instant
      # feedback instead of waiting on a cold Python import.
      reaction_result=$(lark_add_reaction "$message_id" "Typing")
      reaction_id=$(echo "$reaction_result" | jq -r '.reaction_id // .data.reaction_id // empty' 2>/dev/null || true)

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
    os.environ.get('HEARTBEAT_MODEL', 'opus'), work_dir=os.environ.get('WORK_DIR', jd))
runner.run_cycle(force=True, only_task='memory-hourly', lock_wait=120)
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

      # ── Engagement tracking (background — independent of the reply, must not
      # block the ack or dispatch; a fresh Python import here costs ~0.5-2s) ──
      python3 -m core.engagement "$content" >/dev/null 2>>"$LOG_FILE" &

      # Concurrency guard: wait if too many handlers are running
      # Note: `jobs -r` doesn't work reliably inside a pipe subshell.
      # Use /proc-style check: count active session lock files as a proxy.
      while [ "$(find "$JARVIS_DIR" -maxdepth 1 -name '.session_lock_*' 2>/dev/null | wc -l)" -ge "$MAX_HANDLERS" ]; do
        sleep 1
      done

      # ── Merge completed background-job results into this conversation
      # (REQ-16): prepend summaries so the dialog knows what jobs found,
      # instead of the result living only in a notification card.
      _pm_file="$JOBS_DIR/pending_merge.jsonl"
      if [ -f "$_pm_file" ]; then
        _pm_text=$(JV_PM="$_pm_file" JV_KEY="$conv_key" python3 -c "
import json, os
path, key = os.environ['JV_PM'], os.environ['JV_KEY']
keep, mine = [], []
try:
    for line in open(path):
        try:
            e = json.loads(line)
        except Exception:
            continue
        (mine if e.get('conv_key') == key else keep).append(e)
except OSError:
    raise SystemExit
if mine:
    # Small append-vs-rewrite race window with a job finishing right now is
    # acceptable: a lost merge degrades to card-only delivery.
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        for e in keep:
            f.write(json.dumps(e, ensure_ascii=False) + '\n')
    os.replace(tmp, path)
    for e in mine:
        print(f\"[后台任务 {e.get('job_id','')} 已完成 @ {e.get('ts','')}]\n{e.get('summary','')}\n\")
" 2>>"$LOG_FILE")
        if [ -n "$_pm_text" ]; then
          content="${_pm_text}
${content}"
          log_info "Merged pending bg-job result(s) into conversation"
        fi
      fi

      # Dispatch to background — main loop continues immediately
      handle_message "$conv_key" "$content" "$message_id" "$session_id" "$reaction_id" &
      log_info "[$session_id] Dispatched to background handler (PID $!)"
      done
}

while true; do
  run_lark_listener_once
  _listener_rc=$?
  log_warn "Lark listener exited (rc=$_listener_rc) — reconnecting in 5s"
  sleep 5
done
