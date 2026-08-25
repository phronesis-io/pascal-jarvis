#!/usr/bin/env bash
# jarvis-harness: Lark/Feishu bot + heartbeat-driven personal AI agent
#
# - Listens for user messages on Lark
# - Heartbeat loop runs scheduled tasks (feed triage, memory consolidation, etc.)
# - Claude Code handles conversation with full memory injection
#
# Configuration: jarvis.yaml (copy from jarvis.example.yaml)

set -uo pipefail

# Every runtime artifact is private by default. Individual public assets can
# opt into broader permissions explicitly; logs, DBs, prompt snapshots and
# temporary credential files must never inherit a permissive shell umask.
umask 077

# Ensure UTF-8 encoding for Chinese content processing
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd -P)"
export JARVIS_DIR
# shellcheck source=scripts/runtime_env.sh
source "$JARVIS_DIR/scripts/runtime_env.sh"
# shellcheck source=scripts/process_lifecycle.sh
source "$JARVIS_DIR/scripts/process_lifecycle.sh"

# Canonicalize argv before anything else (2026-07-08): every identity check
# in the stack — the session-lock kill case-glob, restart.sh's pkill, and
# daemon.py's _session_lock_pid_is_ours — anchors on the ABSOLUTE
# "$JARVIS_DIR/bot.sh" argv. A manual `./bot.sh` start keeps the relative
# argv, so all of them miss it: locks get deleted while the holder survives,
# and a daemon-driven restart spawns a SECOND bot. exec keeps our PID (the
# pidfile/$$ logic below is unaffected) and cannot loop: after the exec,
# "$0" IS the canonical path. pwd -P above matters — daemon.py resolves
# symlinks (Path.resolve()), so the argv must be the physical path to match.
if [ "$0" != "$JARVIS_DIR/$(basename "$0")" ]; then
  exec bash "$JARVIS_DIR/$(basename "$0")" "$@"
fi

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

# Production processes run one reviewed source snapshot for their whole
# lifetime. A bot started from a feature branch, or a watchdog child respawned
# after the checkout changed, can otherwise load code that never passed CI or
# the release gate. Local development can opt out explicitly.
_BOOT_GIT_HEAD=$(git rev-parse HEAD 2>/dev/null || true)
_ORIGIN_MAIN_HEAD=$(git rev-parse origin/main 2>/dev/null || true)
_RUNTIME_GIT_PATHS=(
  core tasks scripts plugins handlers sources static
  admin.py daemon.py bot.sh restart.sh components.yaml HEARTBEAT.md
)

runtime_source_unchanged() {
  local _current_head _dirty
  _current_head=$(git rev-parse HEAD 2>/dev/null || true)
  [ -n "$_BOOT_GIT_HEAD" ] && [ "$_current_head" = "$_BOOT_GIT_HEAD" ] || return 1
  _dirty=$(git status --porcelain -- "${_RUNTIME_GIT_PATHS[@]}" 2>/dev/null)
  [ -z "$_dirty" ]
}

if [ "${JARVIS_ALLOW_UNRELEASED_RUNTIME:-false}" != "true" ]; then
  if [ -n "$_ORIGIN_MAIN_HEAD" ] && [ "$_BOOT_GIT_HEAD" != "$_ORIGIN_MAIN_HEAD" ]; then
    echo "FATAL: refusing to start Jarvis from an unreleased revision" >&2
    exit 1
  fi
  if ! runtime_source_unchanged; then
    echo "FATAL: refusing to start Jarvis with modified runtime source" >&2
    exit 1
  fi
fi

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
python3 -m core.deploy register bot --pid "$$" >/dev/null 2>&1 \
  || echo "WARN: failed to register bot runtime version" >&2

# ── Process conflict detection ──────────────────────────────────────
# Orphan stream loop first: when a previous bot.sh died without its trap
# (kill -9, crash), its core.ef_stream_loop child survives on init and keeps
# respawning `eigenflux stream`. Killing only the stream child below is
# useless then — the orphan parent respawns it within seconds and the two
# loops trade "Connection replaced by another session" forever (8/22 deploy:
# ef-stream unhealthy, retry 8, until the orphan was killed by hand).
# Same ps+awk exact match as the heartbeat orphan check below.
_orphan_stream=$(ps -eo pid,comm,args | awk '$4 == "-m" && $5 == "core.ef_stream_loop" {print $1}')
if [ -n "$_orphan_stream" ]; then
  echo "WARN: Found orphan ef_stream_loop process(es): $_orphan_stream — killing" >&2
  echo "$_orphan_stream" | xargs kill 2>/dev/null || true
  sleep 1
fi

# Detect competing eigenflux stream processes from openclaw-gateway or
# stale bot instances. Multiple streams cause "Connection replaced" loops.
# Match only actual eigenflux stream processes (not Claude prompts containing the string)
_competing_streams=$(ps -eo pid,comm,args | awk '$2 == "eigenflux" && $4 == "stream" {print $1}' | wc -l | tr -d ' ')
if [ "$_competing_streams" -gt 0 ]; then
  echo "WARN: Found $_competing_streams competing eigenflux stream process(es) — killing" >&2
  ps -eo pid,comm,args | awk '$2 == "eigenflux" && $4 == "stream" {print $1}' | xargs kill 2>/dev/null || true
  sleep 1
fi

# Orphan heartbeats: the double-start guard above proved no other bot.sh is
# alive, so any surviving core.heartbeat_loop belongs to a dead instance and
# runs pre-restart code. Kill it — it holds the singleton flock, and our own
# fresh heartbeat below would otherwise exit on the lock (7/7 incident: a
# 2.5-day-old orphan kept the lock through a guardian restart).
# ps+awk, NOT pgrep -f: same reason as the stream check above — a bare
# substring match also hits Claude/monitor shells that merely MENTION the
# module name, and killing those is friendly fire.
_orphan_hb=$(ps -eo pid,comm,args | awk '$4 == "-m" && $5 == "core.heartbeat_loop" {print $1}')
if [ -n "$_orphan_hb" ]; then
  echo "WARN: Found orphan heartbeat_loop process(es): $_orphan_hb — killing" >&2
  echo "$_orphan_hb" | xargs kill 2>/dev/null || true
  sleep 1
fi

LOG_FILE="$JARVIS_DIR/jarvis.log"
LOG_MAX_BYTES=500000  # 500KB — rotate on startup if exceeded
MEMORY_CACHE_FILE="$JARVIS_DIR/.memory_cache"   # last-known-good memory snapshot

# ── Log rotation (on startup) ────────────────────────────────────────
# Archive 8 generations before truncating — destroyed history made
# failure-rate audits impossible (REQ-80: 3×500KB covered only ~2.5 days;
# jarvis.log lives in the repo dir, so deeper archives are safe from the
# macOS /tmp 3-day sweeper).
if [ -f "$LOG_FILE" ] && [ "$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
  for _gen in 7 6 5 4 3 2 1; do
    mv -f "$LOG_FILE.$_gen" "$LOG_FILE.$((_gen + 1))" 2>/dev/null || true
  done
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

# ── Locked append to engagement_log (multi-writer safety) ──────────
_append_elog() {
  # Usage: echo '{"json":"row"}' | _append_elog
  # Flock on the DATA FILE itself (same target as Python writers and _trim_file).
  local _elog="$JARVIS_DIR/engagement_log.jsonl"
  python3 -c '
import sys, fcntl
elog = sys.argv[1]
with open(elog, "a") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    f.write(sys.stdin.read())
' "$_elog" 2>/dev/null || true
}

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
from core.model_control import harness_environment
c = Config(os.environ["CONFIG_FILE"])
def emit(name, value):
    print(f"{name}={shlex.quote(str(value))}")
emit("USER_ID", c.lark.get("user_id", ""))
emit("APP_ID", c.lark.get("app_id", ""))
emit("OWNER_NAME", c.owner_name)
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
for name, value in harness_environment(c).items():
    emit(name, value)
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

export MEMORY_DIR WORK_DIR CLAUDE_PROJECT_DIR USER_ID OWNER_NAME LOG_FILE MAIN_MODEL HEARTBEAT_MODEL HEARTBEAT_TIMEOUT CHECK_INTERVAL
# Heartbeat memory diet (2026-08-24): the warm knowledge tier rides in every
# heartbeat call as ~60% of the injected payload while most tasks never read
# it. "index" (PR#100) keeps the behavioral rules inline verbatim and turns
# the reference notes into a one-line map the model reads from disk on demand.
# Overridable: JARVIS_WARM_MEMORY_MODE=full in the environment restores the
# inline tier without a code change.
JARVIS_WARM_MEMORY_MODE="${JARVIS_WARM_MEMORY_MODE:-index}"
export JARVIS_WARM_MEMORY_MODE
LARK_APP_ID="${LARK_APP_ID:-${APP_ID:-}}"
export LARK_APP_ID

# Bot's own open_id (REQ-100 group chat): a group @-mention references the
# bot by open_id (ou_...), NOT by APP_ID (cli_...) — matching mentions against
# APP_ID alone would silently ignore every group @. Resolved once at startup;
# empty on failure (the gate then falls back to APP_ID matching only).
BOT_OPEN_ID=""
if [ -n "${LARK_APP_ID:-}" ] && [ -n "${LARK_APP_SECRET:-}" ]; then
  BOT_OPEN_ID=$(LARK_APP_ID="$LARK_APP_ID" LARK_APP_SECRET="$LARK_APP_SECRET" \
    python3 -c 'from core.lark_bot_transport import bot_open_id; print(bot_open_id())' \
    2>/dev/null || true)
fi
if [ -z "$BOT_OPEN_ID" ] && command -v lark-cli &>/dev/null && [ -n "${APP_ID:-}" ]; then
  BOT_OPEN_ID=$(lark-cli api get /open-apis/bot/v3/info --as bot 2>/dev/null \
    | jq -r '.bot.open_id // empty' 2>/dev/null || true)
fi
export BOT_OPEN_ID
export CLAUDE_BACKUP_ENABLED CLAUDE_BACKUP_AUTH_TOKEN CLAUDE_BACKUP_BASE_URL CLAUDE_BACKUP_MODEL
export CLAUDE_BACKUP2_ENABLED CLAUDE_BACKUP2_AUTH_TOKEN CLAUDE_BACKUP2_BASE_URL CLAUDE_BACKUP2_MODEL
export BACKUP_MAX_SESSION_SIZE BACKUP_MAX_MEMORY_CHARS
export CODEX_FALLBACK_ENABLED CODEX_FALLBACK_MODEL CODEX_FALLBACK_BINARY CODEX_FALLBACK_TIMEOUT
export OPENAI_FALLBACK_ENABLED OPENAI_FALLBACK_MODEL OPENAI_BASE_URL OPENAI_USER_AGENT OPENAI_FALLBACK_TIMEOUT OPENAI_FALLBACK_MAX_OUTPUT_TOKENS
if [ -z "${OPENAI_API_KEY:-}" ] && [ -n "${OPENAI_API_KEY_CONFIG:-}" ]; then
  export OPENAI_API_KEY="$OPENAI_API_KEY_CONFIG"
fi
unset OPENAI_API_KEY_CONFIG
# Sidecar event backend (empty = lark-cli default; see plugins/lark/client.sh)
export JARVIS_EVENT_BACKEND LARK_APP_SECRET

# Scrub inherited Anthropic overrides (2026-07-08 P0): the stack was once
# restarted from a shell that still exported the backup relay's ANTHROPIC_*
# (set for manual testing). Primary claude calls deliberately inherit the
# ambient env, so ALL "primary" traffic silently rode the third-party relay
# and the failover gate's recovery probe could never touch the real primary
# — it "cleared" against the relay and reported a false recovery. Nothing
# legitimate needs these ambient: every backup path re-injects them per
# call from CLAUDE_BACKUP_* (the `env` wrappers below, heartbeat.py and
# ef_stream_loop.py env copies). ANTHROPIC_MODEL and the DEFAULT_*_MODEL
# aliases ride along in the relay profile and would steer model selection
# past the MAIN_MODEL pin above, so they go too.
_scrubbed=""
for _var in ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL \
    ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL; do
  if [ -n "${!_var+x}" ]; then
    _scrubbed="$_scrubbed $_var"
    unset "$_var"
  fi
done
if [ -n "$_scrubbed" ]; then
  log_warn "Scrubbed inherited Anthropic env from the startup shell:$_scrubbed — channel selection is provider_state-driven; backup creds are injected per call from CLAUDE_BACKUP_*"
fi
unset _scrubbed
# ANTHROPIC_API_KEY can be a legitimate primary credential, so only scrub
# it when it provably equals the backup token (startup-shell pollution);
# otherwise leave it but warn — it overrides the claude.ai login.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  if [ "$ANTHROPIC_API_KEY" = "${CLAUDE_BACKUP_AUTH_TOKEN:-}" ]; then
    unset ANTHROPIC_API_KEY
    log_warn "Scrubbed inherited ANTHROPIC_API_KEY (identical to the backup token — startup-shell pollution)"
  else
    log_warn "ANTHROPIC_API_KEY is set and takes precedence over the claude.ai login for every primary call — unset it before starting bot.sh unless intentional"
  fi
fi

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

# Clean up stale handlers from a previous hard crash before forgetting their
# identity.  Start-time tokens prevent a recycled PID from being killed.
for _stale_dispatch in "$JARVIS_DIR"/.dispatch_*; do
  [ -f "$_stale_dispatch" ] || continue
  terminate_registered_group "$_stale_dispatch" || true
  rm -f -- "$_stale_dispatch"
done
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
  local max_size="${2:-$MAX_SESSION_SIZE}"
  local context_key="${3:-}"
  JV_TRACKER="$SESSION_TRACKER" JV_SDIR="$CLAUDE_PROJECT_DIR" \
    JV_MAX="$max_size" JV_KEY="$conv_key" JV_CONTEXT_KEY="$context_key" python3 <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["JARVIS_DIR"])
from core.session import SessionManager
sm = SessionManager(os.environ["JV_TRACKER"], os.environ["JV_SDIR"],
                    max_size=int(os.environ["JV_MAX"]))
sid, rotated = sm.get_session(os.environ["JV_KEY"], os.environ.get("JV_CONTEXT_KEY", ""))
if rotated:
    reason = sm.get_state(os.environ["JV_KEY"]).get("rotation_reason", "size")
    print(f"ROTATED {reason}", file=sys.stderr)
print(sid)
PYEOF
}

# ── Memory loader with last-known-good cache ─────────────────────────
# If Python fails, we reuse the cached snapshot so Claude never gets an
# empty memory string (which causes the bot to "forget" everything).
load_memory() {
  local max_chars="${1:-}"
  local fresh
  fresh=$(JV_MEM_MAX="$max_chars" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.memory import load_tiered_memory
mc = os.environ.get('JV_MEM_MAX', '')
print(load_tiered_memory(os.environ['MEMORY_DIR'],
                         max_chars=int(mc) if mc else None))
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
# Reliable delivery for the conversation channel (audit 2026-07-10): the
# reply path used to be a single lark-cli attempt — on failure the finished
# reply vanished with the $reply variable (the exact REQ-11 pain, previously
# fixed only on the heartbeat channel). Mirror core/heartbeat_loop.py
# semantics here: per-attempt timeout (lark-cli has no socket timeout, so a
# half-open connection would otherwise wedge the handler subshell forever),
# (2,5)s backoff retries, then a dead-letter row so daemon.py's independent
# channel can tell the user a reply was lost (stability backlog #7 consumer).
LARK_SEND_TIMEOUT="${LARK_SEND_TIMEOUT:-30}"

# with_fn_timeout <secs> <shell_fn> <args...>
# with_timeout can't wrap shell functions on its coreutils branch (timeout(1)
# execs a binary, which can't see them), so reuse its pure-bash fallback
# pattern. A killed attempt returns 143 → callers treat it as a failure.
with_fn_timeout() {
  local secs="$1"; shift
  "$@" &
  local _wf_cmd_pid=$!
  ( sleep "$secs"; kill "$_wf_cmd_pid" 2>/dev/null ) >/dev/null 2>&1 &
  local _wf_killer_pid=$!
  wait "$_wf_cmd_pid"
  local _wf_rc=$?
  kill "$_wf_killer_pid" 2>/dev/null
  wait "$_wf_killer_pid" 2>/dev/null
  return $_wf_rc
}

# _deadletter_reply <message_id> <content>
# Out-of-band record (core/delivery_deadletter.py producer) consumed by
# daemon.py: keeps message_id + the head of the lost text for forensics and
# for the daemon's own-channel notification. Best-effort — never fails caller.
_deadletter_reply() {
  JV_MID="$1" JV_REPLY="$2" python3 -c "
import os, sys
sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.delivery_deadletter import record_overdue
from core.timeutil import now_local_str
head = ' '.join(os.environ.get('JV_REPLY', '').split())[:120]
mid = os.environ.get('JV_MID') or '-'
record_overdue(os.environ['JARVIS_DIR'], 'reply_send_failed',
               f'mid={mid} 回复前段: {head}', now_local_str())
" 2>>"$LOG_FILE" || true
}

# lark_reply_reliable <message_id> <markdown>
# Timeout-wrapped attempt, then (2,5)s backoff retries (same schedule as the
# heartbeat's SEND_RETRY_DELAYS), then dead-letter. Returns 0 iff delivered.
lark_reply_reliable() {
  local message_id="$1" content="$2" _delay
  with_fn_timeout "$LARK_SEND_TIMEOUT" lark_reply "$message_id" "$content" && return 0
  for _delay in 2 5; do
    log_warn "lark_reply failed (mid=$message_id) — retrying in ${_delay}s"
    sleep "$_delay"
    with_fn_timeout "$LARK_SEND_TIMEOUT" lark_reply "$message_id" "$content" && return 0
  done
  _deadletter_reply "$message_id" "$content"
  return 1
}

# lark_send_reliable <markdown> — same contract for the non-reply sender.
lark_send_reliable() {
  local content="$1" _delay
  with_fn_timeout "$LARK_SEND_TIMEOUT" lark_send "$content" && return 0
  for _delay in 2 5; do
    log_warn "lark_send failed — retrying in ${_delay}s"
    sleep "$_delay"
    with_fn_timeout "$LARK_SEND_TIMEOUT" lark_send "$content" && return 0
  done
  _deadletter_reply "" "$content"
  return 1
}

# Unified pipeline adapters.  The old wrappers above remain callable for
# extracted compatibility tests, but production replies no longer enter their
# private retry/dead-letter path.
delivery_reply_reliable() {
  local message_id="$1" content="$2" _json _state _reason
  _json=$(printf '%s' "$content" | python3 -m core.delivery send \
    --source bot-reply --reply-to "$message_id" --stdin \
    --provider "${JARVIS_DELIVERY_PROVIDER:-}" \
    --model "${JARVIS_DELIVERY_MODEL:-}" 2>>"$LOG_FILE") || return 1
  _state=$(JV_DELIVERY_JSON="$_json" python3 -c \
    'import json,os; print(json.loads(os.environ["JV_DELIVERY_JSON"]).get("state",""))' \
    2>>"$LOG_FILE") || return 1
  _reason=$(JV_DELIVERY_JSON="$_json" python3 -c \
    'import json,os; print(json.loads(os.environ["JV_DELIVERY_JSON"]).get("reason",""))' \
    2>>"$LOG_FILE") || true
  if [ "$_state" = "delivered" ] || { [ "$_state" = "read" ] || [ "$_state" = "acted" ]; }; then
    return 0
  fi
  if [ "$_reason" = "duplicate" ] && [ "$_state" = "delivered" ]; then
    return 0
  fi
  log_warn "reply accepted by delivery pipeline but not yet confirmed (state=$_state reason=$_reason)"
  return 1
}

# run_matter_command <content> <conv_key> <destination> <chat_type>
# A deterministic command is classified before execution. Once classified,
# even a hard process exit must fail closed instead of handing a potentially
# committed command to the model for a second interpretation.
run_matter_command() {
  local content="$1" conv_key="$2" destination_id="$3" chat_type="$4"
  local deterministic output status
  deterministic=$(JV_CONTENT="$content" python3 -c "
import os
from core.matter_bridge import command_would_handle
print('true' if command_would_handle(os.environ.get('JV_CONTENT', '')) else 'false')
" 2>>"$LOG_FILE") || deterministic="unknown"
  output=$(python3 -m core.matter_bridge \
    --content "$content" --conv-key "$conv_key" \
    --destination-id "$destination_id" --chat-type "$chat_type" \
    --tracker "$SESSION_TRACKER" --session-dir "$CLAUDE_PROJECT_DIR" \
    --jarvis-dir "$JARVIS_DIR" \
    2>>"$LOG_FILE")
  status=$?
  if [ "$status" -ne 0 ] && [ "$deterministic" != "false" ]; then
    log_err "Deterministic command process failed after classification (status=$status)"
    printf '%s\n' '{"handled":true,"reply":"会话操作执行时中断了。为避免重复执行，本条不会交给模型；请检查当前会话后再重试。","command_process_error":true}'
    return 0
  fi
  printf '%s' "$output"
  return "$status"
}

delivery_send_reliable() {
  local content="$1" _json _state
  _json=$(printf '%s' "$content" | python3 -m core.delivery send \
    --source bot-notice --attention alert --channel lark --stdin \
    2>>"$LOG_FILE") || return 1
  _state=$(JV_DELIVERY_JSON="$_json" python3 -c \
    'import json,os; print(json.loads(os.environ["JV_DELIVERY_JSON"]).get("state",""))' \
    2>>"$LOG_FILE") || return 1
  case "$_state" in
    queued|attempting|delivered|read|acted|suppressed) return 0 ;;
    *) return 1 ;;
  esac
}

delivery_card_reliable() {
  local card_json="$1" _json _state
  _json=$(printf '%s' "$card_json" | python3 -m core.delivery send \
    --source bot-card --kind card --attention notice --channel lark \
    --urgent --stdin 2>>"$LOG_FILE") || return 1
  _state=$(JV_DELIVERY_JSON="$_json" python3 -c \
    'import json,os; print(json.loads(os.environ["JV_DELIVERY_JSON"]).get("state",""))' \
    2>>"$LOG_FILE") || return 1
  case "$_state" in
    queued|attempting|delivered|read|acted|suppressed) return 0 ;;
    *) return 1 ;;
  esac
}

# Thin wrapper around the unified delivery sender, with a local log line.
send_to_lark() {
  local content="$1"
  [ -z "$content" ] && return
  if ! delivery_send_reliable "$content"; then
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
  delivery_send_reliable \
    "⚠️ 重启中断了 ${_dropped} 条正在处理的消息，请重新发送。" || true
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
  local reply="$1" conv_key="$2" message_id="$3" allow="${4:-1}"
  local context_key="${5:-conversation:$conv_key}" matter_id="${6:-}"
  local source_session_id="${7:-}"
  local action_results=""

  # Extract all action markers
  local actions
  actions=$(echo "$reply" | grep -o '\[ACTION:[^]]*\]' 2>/dev/null || true)

  if [ -z "$actions" ]; then
    printf '%s' "$reply"
    return
  fi

  # REQ-102: shared/untrusted messages must not drive ANY action — Python
  # (calendar/intents/broadcast) or bash (bg jobs). Strip every marker and
  # say so; executing nothing beats silently pretending.
  if [ "$allow" != "1" ]; then
    log_info "Actions suppressed (shared/untrusted chat): ${actions:0:120}"
    printf '%s' "$(JV_REPLY="$reply" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.actions import ActionProcessor
ap = ActionProcessor(os.environ['JARVIS_DIR'], os.environ['MEMORY_DIR'], 'jobs', '')
print(ap.process(os.environ['JV_REPLY'], execute=False))
" 2>>"$LOG_FILE" || printf '%s' "$reply")"
    return
  fi

  # ── Python-handled actions (calendar, task, intent, feed, watchlater, etc.) ──
  # Delegate to core/actions.py for all non-process-control actions.
  # This handles: feed_search, watchlater, heartbeat, calendar_*, task_*,
  # praxis_*, intent_*, schedule_task. Returns cleaned reply with results.
  reply=$(JV_REPLY="$reply" JV_LOG_FILE="$LOG_FILE" JV_CONV_KEY="$conv_key" python3 -c "
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
          JV_CONTEXT_KEY="$context_key" JV_MATTER_ID="$matter_id" \
          JV_SOURCE_SESSION_ID="$source_session_id" \
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
            delivery_card_reliable "$_bg_start_card" || \
              send_to_lark "$_bg_start_body"
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
        # Path traversal guard: job IDs are alphanumeric + dashes only
        if [[ ! "$out_id" =~ ^[a-zA-Z0-9_-]+$ ]]; then
          log_warn "[action] Invalid job_output id: ${out_id:0:40}"
          action_results="${action_results}
Invalid job ID."
        else
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

# Run one Codex turn while publishing its killable wrapper PID through the
# same session lock used by Claude. core.codex_fallback handles SIGTERM by
# terminating Codex's own process group, so owner stop/cancel remains real.
run_codex_locked() {
  local _content="$1" _conv_key="$2" _system_prompt_file="$3"
  local _model="$4" _timeout="$5" _work_dir="$6" _binary="$7"
  local _lock_file="$8" _lock_token="$9" _answer_base="${10}"
  local _context_key="${11:-}"
  local _stdin_file="${_answer_base}.codex.stdin"
  local _stdout_file="${_answer_base}.codex.stdout"
  local _stderr_file="${_answer_base}.codex.stderr"
  local _codex_pid _codex_rc

  printf '%s' "$_content" > "$_stdin_file"
  : > "$_stdout_file"
  : > "$_stderr_file"
  python3 -m core.codex_fallback \
    --conv-key "$_conv_key" \
    --context-key "$_context_key" \
    --system-prompt-file "$_system_prompt_file" \
    --model "$_model" \
    --timeout "$_timeout" \
    --work-dir "$_work_dir" \
    --binary "$_binary" \
    < "$_stdin_file" > "$_stdout_file" 2> "$_stderr_file" &
  _codex_pid=$!

  if ! grep -Fq "$_lock_token" "$_lock_file" 2>/dev/null; then
    kill "$_codex_pid" 2>/dev/null || true
    wait "$_codex_pid" 2>/dev/null || true
    rm -f "$_stdin_file" "$_stdout_file"
    return 143
  fi
  if ! session_lock_publish \
      "$_lock_file" "$_codex_pid" "$_lock_token"; then
    kill "$_codex_pid" 2>/dev/null || true
    wait "$_codex_pid" 2>/dev/null || true
    if grep -Fq "$_lock_token" "$_lock_file" 2>/dev/null; then
      printf 'acquiring %s' "$_lock_token" > "$_lock_file"
    fi
    rm -f "$_stdin_file" "$_stdout_file"
    return 74
  fi
  wait "$_codex_pid" 2>/dev/null
  _codex_rc=$?

  if grep -Fq "$_lock_token" "$_lock_file" 2>/dev/null; then
    printf 'acquiring %s' "$_lock_token" > "$_lock_file"
  else
    # stop/cancel removed the lock. Even a just-finished answer is discarded:
    # once the user cancelled, no late reply or alternate provider may run.
    _codex_rc=143
  fi
  if [ "$_codex_rc" -eq 0 ]; then
    cat "$_stdout_file"
  fi
  rm -f "$_stdin_file" "$_stdout_file"
  return "$_codex_rc"
}

# ── Message Handler (runs in background subshell) ────────────────────
# Extracted from the main loop so different conversations run in parallel.
# Same-session messages serialize via the existing lock file mechanism.
handle_message() {
  local conv_key="$1" content="$2" message_id="$3" session_id="$4"
  local reaction_id="$5" chat_type="${6:-unknown}" sender_id="${7:-}"
  local logical_context_key="${8:-}" matter_id="${9:-}"
  local dispatch_marker="${10:-}"
  local _handler_pid _handler_token
  local _raw_user_content="$content"
  local prompt_chat_type="$chat_type"
  local is_owner_p2p=0

  # Bash 3.2 does not run an EXIT trap reliably when a backgrounded function
  # returns.  Keep normal-return cleanup explicit and reserve the signal trap
  # for interrupted handlers.  The parent writes Bash's authoritative `$!`
  # into the marker; wait for that handoff before starting provider work.
  _handler_pid=$(/bin/sh -c 'printf "%s" "$PPID"')
  _handler_token=$(process_start_token "$_handler_pid" 2>/dev/null || true)
  _finish_message_handler() {
    # Promotion rehome happens in the watchdog subshell; this sidecar carries
    # the new registry path back to the parent handler.
    [ -f "${ANSWER_FILE:-}.dispatch_marker" ] \
      && dispatch_marker=$(cat "${ANSWER_FILE}.dispatch_marker" 2>/dev/null)
    [ -n "$dispatch_marker" ] \
      && dispatch_marker_remove_owned "$dispatch_marker" \
        "$_handler_pid" "$_handler_token"
    # Promotion can be interrupted between publishing the new marker, writing
    # its sidecar, and retiring the old marker.  Remove every registry entry
    # that still carries this exact process identity.
    dispatch_markers_remove_owned "$JARVIS_DIR" \
      "$_handler_pid" "$_handler_token"
    dispatch_marker=""
    [ -n "$reaction_id" ] \
      && lark_remove_reaction "$message_id" "$reaction_id" 2>/dev/null
    reaction_id=""
    trap - EXIT TERM INT
  }
  _abort_message_handler() {
    _finish_message_handler
    if process_group_is_owned "$_handler_pid" "$_handler_token" "$$"; then
      # TERM gives provider wrappers a chance to reap detached tool sessions.
      kill -TERM -- "-$_handler_pid" 2>/dev/null || true
    fi
  }
  trap '_abort_message_handler $?' EXIT
  trap '_abort_message_handler; exit 143' TERM INT
  if [ -n "$dispatch_marker" ]; then
    if ! dispatch_marker_wait_owned "$dispatch_marker" \
      "$_handler_pid" "$_handler_token" 100; then
      log_err "[$session_id] Dispatch marker handoff timed out"
      _finish_message_handler
      return
    fi
  fi

  if [ "$chat_type" = "p2p" ] && [ -n "$sender_id" ] \
      && [ "$sender_id" = "$USER_ID" ]; then
    is_owner_p2p=1
  fi

  # ── Group chat mode (REQ-100~102) ──────────────────────────────────
  # A group session is visible to and drivable by non-owners: it gets the
  # curated group context instead of personal memory (core/prompt.py), a
  # restricted claude tool surface (no Bash/file access — group members must
  # not execute anything on this machine), no action markers, and
  # speaker attribution so the model knows who is talking.
  local is_group=0 allow_actions=1 claude_tool_flags=()
  local openai_fallback_flags=()
  # Fail CLOSED on empty/unknown chat_type (red-team: missing field defaults
  # to p2p → full memory + full tools for a group message). Only an explicit
  # "p2p" unlocks the private path; everything else is treated as a group.
  if [ "$chat_type" != "p2p" ]; then
    is_group=1
    # WebSearch ONLY. WebFetch is deliberately excluded: it can reach
    # localhost (admin console :3456) — a group member could
    # exfiltrate memory/logs through it. Bash/file tools would be code
    # execution on this machine for anyone in the group.
    claude_tool_flags=(--allowedTools "WebSearch" --disallowedTools "Bash,Edit,Write,NotebookEdit,Read,Glob,Grep,Agent,Skill,WebFetch,TaskCreate,TaskUpdate")
    # The final GPT route must preserve the same group trust boundary.
    # Otherwise a Claude outage would silently turn an untrusted group prompt
    # into local bash/file access.
    openai_fallback_flags=(--no-tools)
    # A shared transcript is an untrusted action surface even when the owner
    # authored the current turn: prior group content can steer the model, and
    # private action receipts can disclose names to every member.
    allow_actions=0
    # tail -c: bash 3.2 has no negative substring offsets
    local _sid_tail
    _sid_tail=$(printf '%s' "$sender_id" | tail -c 6)
    local speaker="群成员(${_sid_tail:-unknown})"
    [ "$sender_id" = "$USER_ID" ] && speaker="${OWNER_NAME:-主人}（主人）"
    content="[发言人: $speaker]
$content"
  fi

  # ── Non-owner p2p: same tool restriction as groups ──────────────────
  # Anyone in the Lark org can DM the bot. Without this gate, a non-owner
  # p2p message gets full Bash/file access via --dangerously-skip-permissions.
  if [ "$chat_type" = "p2p" ] && [ "$is_owner_p2p" -eq 0 ]; then
    # Keep direct-message delivery semantics, but build the same
    # privacy-bounded prompt used for shared conversations.
    is_group=1
    prompt_chat_type="external_p2p"
    claude_tool_flags=(--allowedTools "WebSearch" --disallowedTools "Bash,Edit,Write,NotebookEdit,Read,Glob,Grep,Agent,Skill,WebFetch,TaskCreate,TaskUpdate")
    openai_fallback_flags=(--no-tools)
    allow_actions=0
  fi

  # Delegation Phase-0 shadow capture: precision-first and side-effect free.
  # It stores only the stable message reference and a coarse prediction, never
  # the private body. Group messages are excluded because their principals and
  # permissions differ from the owner's private contract.
  if [ "$chat_type" = "p2p" ] && [ -n "$message_id" ]; then
    printf '%s' "$_raw_user_content" \
      | python3 -m core.delegation_shadow capture \
          --source lark \
          --source-ref "$message_id" \
          --principal "${sender_id:-${USER_ID:-owner}}" \
          >/dev/null 2>>"$LOG_FILE" || true
  fi

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
  # Provider-aware memory budget: backup relay has a smaller context window.
  local _mem_budget=""
  local _msg_gate
  _msg_gate=$(python3 -m core.model_fallback --gate 2>/dev/null || echo primary)
  if [ "$_msg_gate" != "primary" ] \
    && [ "${CLAUDE_BACKUP_ENABLED:-true}" = "true" ] \
    && [ -n "${CLAUDE_BACKUP_AUTH_TOKEN:-}" ]; then
    _mem_budget="${BACKUP_MAX_MEMORY_CHARS:-40000}"
  fi
  local sys_prompt
  local _resume_existing=0
  [ -f "$CLAUDE_PROJECT_DIR/${session_id}.jsonl" ] && _resume_existing=1
  sys_prompt=$(printf '%s' "$content" | \
    JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" \
    JV_SID="$session_id" JV_SDIR="$CLAUDE_PROJECT_DIR" JV_CHAT_TYPE="$prompt_chat_type" \
    JV_MEM_MAX="$_mem_budget" JV_CONTEXT_KEY="$logical_context_key" \
    JV_MATTER_ID="$matter_id" JV_RESUME_EXISTING="$_resume_existing" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.prompt import build_system_prompt
from core.timeutil import now_local_str
mc = os.environ.get('JV_MEM_MAX', '')
focus_text = sys.stdin.read()
print(build_system_prompt(
    jarvis_dir=os.environ['JARVIS_DIR'],
    memory_dir=os.environ['MEMORY_DIR'],
    session_dir=os.environ.get('JV_SDIR', ''),
    session_id=os.environ.get('JV_SID', ''),
    conv_key=os.environ.get('JV_KEY', ''),
    now_ts=now_local_str('%Y-%m-%d %H:%M %A'),
    tracker_path=os.environ.get('JV_TRACKER', 'active_sessions.json'),
    chat_type=os.environ.get('JV_CHAT_TYPE', 'p2p'),
    max_memory_chars=int(mc) if mc else None,
    context_key=os.environ.get('JV_CONTEXT_KEY', ''),
    matter_id=os.environ.get('JV_MATTER_ID', ''),
    focus_text=focus_text,
    resume_existing=os.environ.get('JV_RESUME_EXISTING') == '1',
))
" 2>>"$LOG_FILE")

  if [ -z "$sys_prompt" ]; then
    log_warn "[$session_id] System prompt build failed — using fallback"
    if [ "$is_group" -eq 1 ]; then
      # NEVER fall back to personal memory in a group session — a prompt-build
      # failure must not become a privacy breach (REQ-100).
      sys_prompt="你是一个 AI 助手，正在群聊里。简洁回答通用问题；不了解也绝不讨论主人的任何私人信息；不执行任何动作指令。"
    else
      sys_prompt="You are a personal assistant. Reply in the same language the user uses.
$(load_memory "$_mem_budget")"
    fi
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
  # Active lock format is tab-separated provider PID, provider start identity,
  # and an owner token rooted in this handler's PID/start identity.
  local _lock_token
  _lock_token="${_handler_pid}|${_handler_token}|$(date +%s)|$RANDOM"
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
          # Only send if lock is genuinely contended — a short sleep after the
          # check avoids the "notice then instant reply" glitch when the holder
          # releases in the same 5s window we hit the 30s boundary.
          sleep 2
          if [ -f "$LOCK_FILE" ]; then
            _busy_notice_sent=1
            lark_reply_text "$message_id" "前一条还在处理，我已把这条排队；轮到它时会继续，不需要重发。" >/dev/null 2>&1 || true
          fi
        fi
      fi
      sleep 5
      waited=$((waited + 5))
    done
    # Re-resolve after acquiring: the conversation may have rotated to a new
    # session while we waited (background-job auto-promotion does this). Our
    # session_id was resolved at dispatch time — resuming it now would write
    # into the transcript the promoted Claude is still appending to.
    _cur_state=$(JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" python3 -c "
import json, os
try:
    state=json.load(open(os.environ['JV_TRACKER'])).get(os.environ['JV_KEY'], {})
    print(state.get('session_id', ''))
    print(state.get('context_key', ''))
except Exception:
    print('')
    print('')
" 2>/dev/null || echo "")
    _cur_sid=$(printf '%s\n' "$_cur_state" | sed -n '1p')
    _cur_context=$(printf '%s\n' "$_cur_state" | sed -n '2p')
    if [ -n "$_cur_sid" ] && [ "$_cur_sid" != "$session_id" ]; then
      if [ -n "$_cur_context" ] && [ "$_cur_context" != "$logical_context_key" ]; then
        log_warn "[$session_id] Refusing queued turn after logical context changed to $_cur_context"
        rm -f "$LOCK_FILE"
        rm -f "$ANSWER_FILE" "$SYS_PROMPT_FILE" \
          "${ANSWER_FILE}.stderr" "${ANSWER_FILE}.watchdog" \
          "${ANSWER_FILE}.promoted" "${ANSWER_FILE}.codex.stderr" \
          "${ANSWER_FILE}.codex.stdin" "${ANSWER_FILE}.codex.stdout" \
          "${ANSWER_FILE}.openai.stderr"
        delivery_reply_reliable "$message_id" \
          "这条排队消息所属的会话已经切换，因此没有跨会话执行。请切回原会话后重发。" || true
        _finish_message_handler
        return
      fi
      log_info "[$session_id] Conversation rotated to $_cur_sid while waiting — switching"
      rm -f "$LOCK_FILE"
      session_id="$_cur_sid"
      LOCK_FILE="$JARVIS_DIR/.session_lock_${session_id}"
      # Rebuild system prompt with the new session's recent turns
      _resume_existing=0
      [ -f "$CLAUDE_PROJECT_DIR/${session_id}.jsonl" ] && _resume_existing=1
      sys_prompt=$(printf '%s' "$content" | \
        JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" \
        JV_SID="$session_id" JV_SDIR="$CLAUDE_PROJECT_DIR" JV_CHAT_TYPE="$prompt_chat_type" \
        JV_MEM_MAX="$_mem_budget" JV_CONTEXT_KEY="$logical_context_key" \
        JV_MATTER_ID="$matter_id" JV_RESUME_EXISTING="$_resume_existing" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.prompt import build_system_prompt
from core.timeutil import now_local_str
mc = os.environ.get('JV_MEM_MAX', '')
focus_text = sys.stdin.read()
print(build_system_prompt(
    jarvis_dir=os.environ['JARVIS_DIR'],
    memory_dir=os.environ['MEMORY_DIR'],
    session_dir=os.environ.get('JV_SDIR', ''),
    session_id=os.environ.get('JV_SID', ''),
    conv_key=os.environ.get('JV_KEY', ''),
    now_ts=now_local_str('%Y-%m-%d %H:%M %A'),
    tracker_path=os.environ.get('JV_TRACKER', 'active_sessions.json'),
    chat_type=os.environ.get('JV_CHAT_TYPE', 'p2p'),
    max_memory_chars=int(mc) if mc else None,
    context_key=os.environ.get('JV_CONTEXT_KEY', ''),
    matter_id=os.environ.get('JV_MATTER_ID', ''),
    focus_text=focus_text,
    resume_existing=os.environ.get('JV_RESUME_EXISTING') == '1',
))
" 2>>"$LOG_FILE") || true
      [ -n "$sys_prompt" ] && printf '%s' "$sys_prompt" > "$SYS_PROMPT_FILE"
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
  local _claude_backup2_tried=0
  local _claude_backup_token="${CLAUDE_BACKUP_AUTH_TOKEN:-}"
  local _claude_backup_base_url="${CLAUDE_BACKUP_BASE_URL:-}"
  local _codex_tried=0
  local _codex_uncertain=0
  local _codex_cancelled=0
  local _openai_tried=0
  local _answer_provider=""
  local _answer_model=""
  # REQ-77: the model to use this attempt. Degrades (opus→sonnet→haiku) if a
  # spawn fails with a model-unavailable / spend-limit stderr, instead of
  # looping to empty death ("Continue / No response requested").
  local _cur_model="$MAIN_MODEL"
  local _active_claude_provider="primary"

  # Account-limit state and route health answer different questions: the
  # former says primary must not be retried; the latter says which configured
  # fallback has answered recently. A failed relay gets a short cooldown so
  # every new Lark turn does not pay for the same known 424 again.
  local _provider_gate="primary"
  local _health_route=""
  local _health_routed=0
  local _no_healthy_provider=0
  _provider_gate=$(python3 -m core.model_fallback --gate 2>/dev/null || echo primary)
  local _route_context="owner_chat"
  [ "$is_group" -eq 1 ] && _route_context="group"
  _health_route=$(python3 -m core.provider_health route \
    --context "$_route_context" --gate "$_provider_gate" \
    2>/dev/null || true)
  case "$_health_route" in
    none) _health_routed=1; _no_healthy_provider=1 ;;
  esac

  # A private conversation may explicitly prefer Codex. This changes only
  # routing order: if Codex is unavailable, the normal Claude chain still
  # runs, and the provider/model ledger records whichever route answered.
  local _provider_preference="auto"
  if [ "$is_group" -ne 1 ]; then
    _provider_preference=$(python3 -m core.runtime_provider get "$conv_key" \
      2>>"$LOG_FILE" || echo auto)
  fi
  if [ "$_provider_preference" = "auto" ] \
    && { [ "$_health_route" = "codex" ] || [ "$_health_route" = "openai" ]; }; then
    _provider_preference="$_health_route"
    _health_routed=1
    log_info "[$session_id] Provider health route: skipping cooling relay and trying $_health_route"
  fi
  if [ "$_provider_preference" = "codex" ] \
    && [ "${CODEX_FALLBACK_ENABLED:-true}" = "true" ]; then
    _codex_tried=1
    log_info "[$session_id] Conversation preference: trying Codex first (${CODEX_FALLBACK_MODEL:-gpt-5.5})"
    answer=$(run_codex_locked "$content" "$conv_key" "$SYS_PROMPT_FILE" \
      "${CODEX_FALLBACK_MODEL:-gpt-5.5}" \
      "${CODEX_FALLBACK_TIMEOUT:-300}" "$WORK_DIR" \
      "${CODEX_FALLBACK_BINARY:-}" "$LOCK_FILE" "$_lock_token" \
      "$ANSWER_FILE" "$logical_context_key")
    _codex_exit=$?
    if [ "$_codex_exit" -eq 0 ] && [ -n "$answer" ]; then
      _answer_provider="Codex"
      _answer_model="${CODEX_FALLBACK_MODEL:-gpt-5.5}"
      python3 -m core.provider_health observe codex healthy \
        --detail request_succeeded >/dev/null 2>&1 || true
      log_info "[$session_id] Preferred Codex route succeeded (${#answer} chars)"
    elif [ "$_codex_exit" -eq 75 ]; then
      answer=""
      python3 -m core.provider_health observe codex unhealthy \
        --detail request_failed >/dev/null 2>&1 || true
      _codex_err=$(head -5 "${ANSWER_FILE}.codex.stderr" 2>/dev/null | tr '\n' ' ')
      log_warn "[$session_id] Preferred Codex route failed (exit=$_codex_exit, stderr=${_codex_err:-none}) — continuing to Claude"
    elif [ "$_codex_exit" -eq 143 ]; then
      answer=""
      _codex_cancelled=1
      log_info "[$session_id] Preferred Codex route cancelled — no replay"
    else
      answer=""
      _codex_uncertain=1
      _codex_err=$(head -5 "${ANSWER_FILE}.codex.stderr" 2>/dev/null | tr '\n' ' ')
      log_warn "[$session_id] Preferred Codex route ended ambiguously (exit=$_codex_exit, stderr=${_codex_err:-none}) — refusing automatic replay"
    fi
  fi

  # After any safe Codex pre-return failure, re-elect from routes outside
  # cooldown. This also covers an explicit per-conversation Codex preference:
  # it must not fall back to an account-limited primary when every fallback is
  # cooling. An empty route means the health command itself failed, so the
  # historical bounded chain remains the fail-soft path.
  if [ -z "$answer" ] \
    && [ "$_codex_uncertain" -eq 0 ] && [ "$_codex_cancelled" -eq 0 ]; then
    _health_route=$(python3 -m core.provider_health route \
      --context "$_route_context" --gate "$_provider_gate" \
      2>/dev/null || true)
    case "$_health_route" in
      codex|openai) _health_routed=1 ;;
      none) _health_routed=1; _no_healthy_provider=1 ;;
      *) _health_routed=0 ;;
    esac
  fi
  if [ "$_health_routed" -eq 1 ] && [ -z "$answer" ] \
    && [ "$_health_route" = "openai" ] \
    && [ "${OPENAI_FALLBACK_ENABLED:-true}" = "true" ] \
    && [ -n "${OPENAI_API_KEY:-}" ]; then
    _openai_tried=1
    log_warn "[$session_id] Provider health route: trying OpenAI API fallback (${OPENAI_FALLBACK_MODEL:-gpt-5.5})"
    answer=$(printf '%s' "$content" | JV_SYSTEM_PROMPT_FILE="$SYS_PROMPT_FILE" \
      python3 -m core.openai_fallback \
      ${openai_fallback_flags[@]+"${openai_fallback_flags[@]}"} \
      2>"${ANSWER_FILE}.openai.stderr")
    _openai_exit=$?
    if [ "$_openai_exit" -eq 0 ] && [ -n "$answer" ]; then
      _answer_provider="GPT fallback"
      _answer_model="${OPENAI_FALLBACK_MODEL:-gpt-5.5}"
      python3 -m core.provider_health observe openai healthy \
        --detail request_succeeded >/dev/null 2>&1 || true
    else
      answer=""
      python3 -m core.provider_health observe openai unhealthy \
        --detail request_failed >/dev/null 2>&1 || true
    fi
  fi

  # Start from the route selected by the account gate plus real-request health.
  # Account-wide spend/session limits used to make every
  # message re-probe primary from scratch —
  # ~11s of doomed attempts per reply AND two raw error turns written into
  # the live session transcript each time. When the gate says the flag is
  # fresh, start attempt 1 on backup; _claude_backup_tried=1 keeps the
  # OpenAI rung reachable if backup itself fails. 'probe' elects this
  # message to try primary once — success clears the flag below.
  if { [ "$_health_route" = "backup1" ] \
      || { [ "$_provider_gate" = "backup" ] && [ -z "$_health_route" ]; }; } \
    && [ "$_health_routed" -eq 0 ] \
    && [ "${CLAUDE_BACKUP_ENABLED:-true}" = "true" ] \
    && [ -n "${CLAUDE_BACKUP_AUTH_TOKEN:-}" ] \
    && [ -n "${CLAUDE_BACKUP_BASE_URL:-}" ]; then
    _use_claude_backup=1
    _claude_backup_tried=1
    _active_claude_provider="backup1"
    _cur_model="${CLAUDE_BACKUP_MODEL:-$MAIN_MODEL}"
    log_info "[$session_id] Model route: starting on backup provider (model=$_cur_model, primary_gate=$_provider_gate)"
  elif { [ "$_health_route" = "backup2" ] \
      || { [ "$_provider_gate" = "backup" ] && [ -z "$_health_route" ]; }; } \
    && [ "$_health_routed" -eq 0 ] \
    && [ "${CLAUDE_BACKUP2_ENABLED:-false}" = "true" ] \
    && [ -n "${CLAUDE_BACKUP2_AUTH_TOKEN:-}" ] \
    && [ -n "${CLAUDE_BACKUP2_BASE_URL:-}" ]; then
    _use_claude_backup=1
    _claude_backup2_tried=1
    _active_claude_provider="backup2"
    _cur_model="${CLAUDE_BACKUP2_MODEL:-$MAIN_MODEL}"
    _claude_backup_token="$CLAUDE_BACKUP2_AUTH_TOKEN"
    _claude_backup_base_url="$CLAUDE_BACKUP2_BASE_URL"
    log_info "[$session_id] Model route: starting on backup2 provider (model=$_cur_model, primary_gate=$_provider_gate)"
  fi

  # Five bounded calls are enough for the longest route:
  # primary opus → sonnet → haiku → Backup 1 → Backup 2. Codex, then the
  # text/API fallback, are invoked after the final Claude-compatible route.
  local _attempt_sequence="1 2 3 4 5"
  { [ -n "$answer" ] || [ "$_health_routed" -eq 1 ] \
      || [ "$_codex_uncertain" -eq 1 ] \
      || [ "$_codex_cancelled" -eq 1 ]; } && _attempt_sequence=""
  for _attempt in $_attempt_sequence; do
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
          ANTHROPIC_AUTH_TOKEN="$_claude_backup_token" \
          ANTHROPIC_BASE_URL="$_claude_backup_base_url" \
          claude -p \
          --resume "$session_id" \
          --model "$_cur_model" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          ${claude_tool_flags[@]+"${claude_tool_flags[@]}"} \
          --output-format json \
          2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
      else
        log_info "[$session_id] Calling primary Claude Code model=$_cur_model"
        (cd "$WORK_DIR" && printf '%s' "$content" | claude -p \
          --resume "$session_id" \
          --model "$_cur_model" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          ${claude_tool_flags[@]+"${claude_tool_flags[@]}"} \
          --output-format json \
          2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
      fi
    else
      [ "$_attempt" -eq 1 ] && log_info "[$session_id] New session"
      if [ "$_use_claude_backup" -eq 1 ]; then
        log_warn "[$session_id] Calling Claude Code backup provider model=$_cur_model"
        (cd "$WORK_DIR" && printf '%s' "$content" | env \
          ANTHROPIC_AUTH_TOKEN="$_claude_backup_token" \
          ANTHROPIC_BASE_URL="$_claude_backup_base_url" \
          claude -p \
          --session-id "$session_id" \
          --model "$_cur_model" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          ${claude_tool_flags[@]+"${claude_tool_flags[@]}"} \
          --output-format json \
          2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
      else
        log_info "[$session_id] Calling primary Claude Code model=$_cur_model"
        (cd "$WORK_DIR" && printf '%s' "$content" | claude -p \
          --session-id "$session_id" \
          --model "$_cur_model" \
          --append-system-prompt "$sys_prompt" \
          --dangerously-skip-permissions \
          ${claude_tool_flags[@]+"${claude_tool_flags[@]}"} \
          --output-format json \
          2>"${ANSWER_FILE}.stderr" > "$ANSWER_FILE") &
      fi
    fi
    _claude_pid=$!
    if ! session_lock_publish \
        "$LOCK_FILE" "$_claude_pid" "$_lock_token"; then
      log_err "[$session_id] Could not publish provider process identity"
      kill "$_claude_pid" 2>/dev/null || true
      wait "$_claude_pid" 2>/dev/null || true
      if grep -Fq "$_lock_token" "$LOCK_FILE" 2>/dev/null; then
        printf 'acquiring %s' "$_lock_token" > "$LOCK_FILE"
      fi
      answer=""
      continue
    fi
    # One natural progress line, then release the conversation to a background
    # job if the call stays slow. Internal tools, retries and IDs stay private.
    (_elapsed=0
     _ack_sent=0
     # Responsiveness policy is single-sourced + tested in core/responsiveness
     # (REQ-59). Pull the tuned constants here; fall back to literals if the
     # module call ever fails so the loop never breaks.
     eval "$(python3 -m core.responsiveness env 2>/dev/null)"
     : "${JV_POLL_FIRST:=10}" "${JV_POLL_STEADY:=10}" \
       "${JV_ACK_AFTER:=20}" "${JV_PROMOTE_AFTER:=90}" \
       "${JV_PROGRESS_ACK:=我还在处理，查清楚后马上告诉你。}"
     _poll="$JV_POLL_FIRST"
     while [ "$_elapsed" -lt 6000 ]; do
       sleep "$_poll"
       _elapsed=$((_elapsed + _poll))
       _poll="$JV_POLL_STEADY"
       if ! kill -0 $_claude_pid 2>/dev/null; then break; fi
       if [ "$_ack_sent" -eq 0 ] && [ "$_elapsed" -ge "$JV_ACK_AFTER" ]; then
         lark_reply_text "$message_id" "$JV_PROGRESS_ACK" >/dev/null 2>&1 || true
         _ack_sent=1
       fi
       # Tool activity remains observable in the private session log.
       # ── Auto-promotion (REQ-16 MVP-2): a call still running at the tested
       # responsiveness threshold becomes a
       # background job. Release the conversation instead of blocking it —
       # "一跑跑3个小时我就用不了这个机器人" was the single harshest complaint
       # in the interaction audit. The conversation rotates to a fresh session
       # so new messages never resume the transcript this Claude still writes;
       # the result comes back via the normal reply + pending_merge.
       # Auto-promotion disabled in groups: the bg job runs with full memory +
       # full tools (no tool restrictions), and its output merges back to the
       # group conv_key visible to non-owners. The correct fix is making bg
       # jobs inherit tool restrictions, but that needs a run_background_job
       # refactor; for now, group long-running calls stay inline and timeout.
       if [ "$is_group" -ne 1 ] && [ "$_elapsed" -ge "$JV_PROMOTE_AFTER" ] && [ ! -f "${ANSWER_FILE}.promoted" ] && kill -0 $_claude_pid 2>/dev/null; then
         _bg_job_id=$(JV_JOBS_DIR="$JOBS_DIR" JV_CONV_KEY="$conv_key" \
           JV_DESC="auto-promoted: ${content:0:120}" JV_MSG_ID="$message_id" \
           JV_CONTEXT_KEY="$logical_context_key" JV_MATTER_ID="$matter_id" \
           JV_SOURCE_SESSION_ID="$session_id" \
           python3 "$JARVIS_DIR/core/jobs.py" create 2>>"$LOG_FILE")
         if [ -n "$_bg_job_id" ]; then
           JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" set-pid "$_bg_job_id" "$_claude_pid" \
             2>>"$LOG_FILE" || true
           printf '%s' "$_bg_job_id" > "${ANSWER_FILE}.promoted"
           if JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" JV_SDIR="$CLAUDE_PROJECT_DIR" \
             JV_CONTEXT_KEY="$logical_context_key" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.session import SessionManager
sm = SessionManager(os.environ['JV_TRACKER'], os.environ['JV_SDIR'])
sm.force_rotate(os.environ['JV_KEY'],
                context_key=os.environ.get('JV_CONTEXT_KEY', ''),
                reason='promotion')
" 2>>"$LOG_FILE"; then
             # Release only OUR lock (ownership may already have moved)
             if grep -q "$_lock_token" "$LOCK_FILE" 2>/dev/null; then
               rm -f "$LOCK_FILE"
             fi
             # The turn is now an independently scoped background job.  Let
             # Pascal switch logical sessions while it finishes; its result is
             # queued against the captured context and cannot leak elsewhere.
             _promoted_marker="$JARVIS_DIR/.dispatch_job_${_bg_job_id}_${_handler_pid}"
             if dispatch_marker_handoff_owned "$dispatch_marker" \
               "$_promoted_marker" "${ANSWER_FILE}.dispatch_marker" \
               "$_handler_pid" "$_handler_token"; then
               dispatch_marker="$_promoted_marker"
             else
               log_warn "[$session_id] Promotion marker rehome failed — retaining conversation marker"
             fi
             lark_reply_text "$message_id" "这件事比预期久，我先放到后台继续做。你可以接着聊，做完我会回来告诉你。" >/dev/null 2>&1 || true
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
      # Sticky provider gate: a HARD account limit from PRIMARY is account-wide
      # — persist it so every caller (replies, heartbeat, background jobs)
      # starts on backup instead of re-walking the doomed primary ladder.
      # --trip also pages Pascal once per outage episode (never again for the
      # same ongoing episode; 6h anti-flap across episodes) via the daemon's
      # Claude-independent dead-letter channel.
      _account_limit_reason=""
      if [ "$_use_claude_backup" -eq 0 ]; then
        _account_limit_reason=$(printf '%s' "$_model_error_text" \
          | python3 -m core.model_fallback --limit-reason 2>/dev/null) || true
      fi
      if [ -n "$_account_limit_reason" ]; then
        python3 -m core.model_fallback --trip "$_account_limit_reason" >/dev/null 2>&1 || true
        python3 -m core.provider_health observe primary unhealthy \
          --detail account_limit >/dev/null 2>&1 || true
      fi
      # REQ-77: if the empty answer was a MODEL error (unavailable / banned /
      # rate-limited) rather than a transient blip, degrade the model for the
      # next attempt instead of retrying the same broken model to death.
      # A HARD account limit yields NO same-provider fallback (empty _fallback):
      # degrading opus→haiku on an exhausted account just burned a second
      # doomed call — the elif below jumps straight to the backup provider.
      _fallback=""
      _provider_failure_reason=$(printf '%s' "$_model_error_text" \
        | python3 -m core.provider_health classify 2>/dev/null) || true
      : "${_provider_failure_reason:=request_failed}"
      if [ "$_use_claude_backup" -eq 0 ]; then
        _fallback=$(printf '%s' "$_model_error_text" | python3 -m core.model_fallback "$_cur_model" 2>/dev/null)
      fi
      if [ "$_provider_failure_reason" != "request_failed" ]; then
        python3 -m core.provider_health observe "$_active_claude_provider" unhealthy \
          --detail "$_provider_failure_reason" >/dev/null 2>&1 || true
      fi
      if [ -n "$_fallback" ]; then
        log_warn "[$session_id] Model error on $_cur_model → degrading to $_fallback (REQ-77)"
        _cur_model="$_fallback"
      elif [ "$_use_claude_backup" -eq 0 ] \
        && [ "${CLAUDE_BACKUP_ENABLED:-true}" = "true" ] \
        && [ "$_claude_backup_tried" -eq 0 ] \
        && [ -n "${CLAUDE_BACKUP_AUTH_TOKEN:-}" ] \
        && [ -n "${CLAUDE_BACKUP_BASE_URL:-}" ] \
        && printf '%s' "$_model_error_text" | python3 -m core.model_fallback --is-preexecution-error 2>/dev/null; then
        _claude_backup_tried=1
        _use_claude_backup=1
        _active_claude_provider="backup1"
        _claude_backup_token="$CLAUDE_BACKUP_AUTH_TOKEN"
        _claude_backup_base_url="$CLAUDE_BACKUP_BASE_URL"
        # Log BEFORE resetting _cur_model: the old order reported "exhausted
        # on opus" even when haiku was the model that actually failed.
        log_warn "[$session_id] Primary Claude exhausted on $_cur_model → trying Claude Code backup provider"
        _cur_model="${CLAUDE_BACKUP_MODEL:-$MAIN_MODEL}"
        # Rebuild system prompt with backup memory budget to avoid oversized context
        _mem_budget="${BACKUP_MAX_MEMORY_CHARS:-40000}"
        _resume_existing=0
        [ -f "$CLAUDE_PROJECT_DIR/${session_id}.jsonl" ] && _resume_existing=1
        sys_prompt=$(printf '%s' "$content" | \
          JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" \
          JV_SID="$session_id" JV_SDIR="$CLAUDE_PROJECT_DIR" JV_CHAT_TYPE="$prompt_chat_type" \
          JV_MEM_MAX="$_mem_budget" JV_CONTEXT_KEY="$logical_context_key" \
          JV_MATTER_ID="$matter_id" JV_RESUME_EXISTING="$_resume_existing" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.prompt import build_system_prompt
from core.timeutil import now_local_str
mc = os.environ.get('JV_MEM_MAX', '')
focus_text = sys.stdin.read()
print(build_system_prompt(
    jarvis_dir=os.environ['JARVIS_DIR'],
    memory_dir=os.environ['MEMORY_DIR'],
    session_dir=os.environ.get('JV_SDIR', ''),
    session_id=os.environ.get('JV_SID', ''),
    conv_key=os.environ.get('JV_KEY', ''),
    now_ts=now_local_str('%Y-%m-%d %H:%M %A'),
    tracker_path=os.environ.get('JV_TRACKER', 'active_sessions.json'),
    chat_type=os.environ.get('JV_CHAT_TYPE', 'p2p'),
    max_memory_chars=int(mc) if mc else None,
    context_key=os.environ.get('JV_CONTEXT_KEY', ''),
    matter_id=os.environ.get('JV_MATTER_ID', ''),
    focus_text=focus_text,
    resume_existing=os.environ.get('JV_RESUME_EXISTING') == '1',
))
" 2>>"$LOG_FILE") || true
        [ -n "$sys_prompt" ] && printf '%s' "$sys_prompt" > "$SYS_PROMPT_FILE"
      elif [ "${CLAUDE_BACKUP2_ENABLED:-false}" = "true" ] \
        && [ "$_claude_backup2_tried" -eq 0 ] \
        && [ -n "${CLAUDE_BACKUP2_AUTH_TOKEN:-}" ] \
        && [ -n "${CLAUDE_BACKUP2_BASE_URL:-}" ] \
        && { [ "$_claude_backup_tried" -eq 1 ] \
             || [ "${CLAUDE_BACKUP_ENABLED:-true}" != "true" ] \
             || [ -z "${CLAUDE_BACKUP_AUTH_TOKEN:-}" ] \
             || [ -z "${CLAUDE_BACKUP_BASE_URL:-}" ]; }; then
        _claude_backup2_tried=1
        _use_claude_backup=1
        python3 -m core.provider_health observe "$_active_claude_provider" unhealthy \
          --detail "$_provider_failure_reason" >/dev/null 2>&1 || true
        _active_claude_provider="backup2"
        log_warn "[$session_id] Backup1 exhausted on $_cur_model → trying Claude Code backup2 provider"
        _cur_model="${CLAUDE_BACKUP2_MODEL:-$MAIN_MODEL}"
        _claude_backup_token="$CLAUDE_BACKUP2_AUTH_TOKEN"
        _claude_backup_base_url="$CLAUDE_BACKUP2_BASE_URL"
      elif { { [ "${CODEX_FALLBACK_ENABLED:-true}" = "true" ] \
                && [ "$is_group" -ne 1 ] \
                && [ "$_codex_tried" -eq 0 ]; } \
             || { [ "${OPENAI_FALLBACK_ENABLED:-true}" = "true" ] \
                  && [ -n "${OPENAI_API_KEY:-}" ] \
                  && [ "$_openai_tried" -eq 0 ]; }; } \
        && { printf '%s' "$_model_error_text" | python3 -m core.model_fallback --is-preexecution-error 2>/dev/null \
             || { { [ "$_claude_backup_tried" -eq 1 ] \
                    || [ "$_claude_backup2_tried" -eq 1 ]; } \
                  && [ "$_attempt" -ge 2 ] \
                  && [ -n "${_model_error_text//[[:space:]]/}" ]; }; }; then
        # The || arm: an auth/network error from the backup relay matches no
        # model-error signature (kept tight after the red-team fix), but once
        # backup has been tried primary is known-dead — dead-ending here left
        # a silent total outage while a working OpenAI route existed.
        # _attempt >= 2 (2026-07-08 red-team fix): the gate PRESETS
        # _claude_backup_tried=1 before attempt 1, so without it one transient
        # relay blip skipped the whole retry ladder and answered with a
        # context-free GPT reply; requiring a completed failed attempt means
        # backup really failed at least once in THIS handler run.
        if [ "${CODEX_FALLBACK_ENABLED:-true}" = "true" ] \
          && [ "$is_group" -ne 1 ] && [ "$_codex_tried" -eq 0 ]; then
          _codex_tried=1
          if [ "$_use_claude_backup" -eq 1 ]; then
            python3 -m core.provider_health observe "$_active_claude_provider" unhealthy \
              --detail "$_provider_failure_reason" >/dev/null 2>&1 || true
          fi
          log_warn "[$session_id] Claude model chain exhausted on $_cur_model → trying Codex fallback (${CODEX_FALLBACK_MODEL:-gpt-5.5})"
          answer=$(run_codex_locked "$content" "$conv_key" \
            "$SYS_PROMPT_FILE" "${CODEX_FALLBACK_MODEL:-gpt-5.5}" \
            "${CODEX_FALLBACK_TIMEOUT:-300}" "$WORK_DIR" \
            "${CODEX_FALLBACK_BINARY:-}" "$LOCK_FILE" "$_lock_token" \
            "$ANSWER_FILE" "$logical_context_key")
          _codex_exit=$?
          if [ "$_codex_exit" -eq 0 ] && [ -n "$answer" ]; then
            _answer_provider="Codex"
            _answer_model="${CODEX_FALLBACK_MODEL:-gpt-5.5}"
            python3 -m core.provider_health observe codex healthy \
              --detail request_succeeded >/dev/null 2>&1 || true
            log_warn "[$session_id] Codex fallback succeeded (${#answer} chars)"
            break
          fi
          answer=""
          if [ "$_codex_exit" -eq 143 ]; then
            _codex_cancelled=1
            log_info "[$session_id] Codex fallback cancelled — no replay"
            break
          fi
          _codex_err=$(head -5 "${ANSWER_FILE}.codex.stderr" 2>/dev/null | tr '\n' ' ')
          python3 -m core.provider_health observe codex unhealthy \
            --detail request_failed >/dev/null 2>&1 || true
          log_warn "[$session_id] Codex fallback failed (exit=$_codex_exit, stderr=${_codex_err:-none})"
          if [ "$_codex_exit" -ne 75 ]; then
            # A timeout/nonzero run may already have executed tools. Replaying
            # the same request through GPT could duplicate external actions.
            _codex_uncertain=1
            break
          fi
        fi
        if [ "${OPENAI_FALLBACK_ENABLED:-true}" = "true" ] \
          && [ -n "${OPENAI_API_KEY:-}" ] && [ "$_openai_tried" -eq 0 ]; then
          _openai_tried=1
          log_warn "[$session_id] Codex unavailable → trying OpenAI API fallback (${OPENAI_FALLBACK_MODEL:-gpt-5.5})"
          answer=$(printf '%s' "$content" | JV_SYSTEM_PROMPT_FILE="$SYS_PROMPT_FILE" \
            python3 -m core.openai_fallback \
            ${openai_fallback_flags[@]+"${openai_fallback_flags[@]}"} \
            2>"${ANSWER_FILE}.openai.stderr")
          _openai_exit=$?
          if [ "$_openai_exit" -eq 0 ] && [ -n "$answer" ]; then
            _answer_provider="GPT fallback"
            _answer_model="${OPENAI_FALLBACK_MODEL:-gpt-5.5}"
            python3 -m core.provider_health observe openai healthy \
              --detail request_succeeded >/dev/null 2>&1 || true
            log_warn "[$session_id] OpenAI fallback succeeded (${#answer} chars)"
            break
          fi
          _openai_err=$(head -5 "${ANSWER_FILE}.openai.stderr" 2>/dev/null | tr '\n' ' ')
          python3 -m core.provider_health observe openai unhealthy \
            --detail request_failed >/dev/null 2>&1 || true
          log_warn "[$session_id] OpenAI fallback failed (exit=$_openai_exit, stderr=${_openai_err:-none})"
        fi
        # Every independent route has now had one bounded attempt. Repeating a
        # relay or uncertain tool-capable turn could duplicate side effects.
        break
      fi
      # On first failure, session file may have been created — update for retry
      session_file="$CLAUDE_PROJECT_DIR/${session_id}.jsonl"
    else
      if [ "$_claude_backup2_tried" -eq 1 ]; then
        _answer_provider="Claude backup2"
      elif [ "$_use_claude_backup" -eq 1 ]; then
        _answer_provider="Claude backup"
      else
        _answer_provider="Claude primary"
        if [ "$_provider_gate" != "primary" ]; then
          # Elected probe succeeded on primary while the spend-limit flag was
          # set — reopen primary for every process (also pages Pascal 恢复了).
          python3 -m core.model_fallback --clear >/dev/null 2>&1 || true
        fi
      fi
      _answer_model="$_cur_model"
      python3 -m core.provider_health observe "$_active_claude_provider" healthy \
        --detail request_succeeded >/dev/null 2>&1 || true
      break
    fi
  done

  local _promoted_job=""
  [ -f "${ANSWER_FILE}.promoted" ] && _promoted_job=$(cat "${ANSWER_FILE}.promoted" 2>/dev/null)
  [ -f "${ANSWER_FILE}.dispatch_marker" ] \
    && dispatch_marker=$(cat "${ANSWER_FILE}.dispatch_marker" 2>/dev/null)
  rm -f "$ANSWER_FILE" "${ANSWER_FILE}.stderr" "${ANSWER_FILE}.watchdog" \
    "${ANSWER_FILE}.promoted" "${ANSWER_FILE}.codex.stderr" \
    "${ANSWER_FILE}.codex.stdin" "${ANSWER_FILE}.codex.stdout" \
    "${ANSWER_FILE}.openai.stderr" "${ANSWER_FILE}.dispatch_marker" "$SYS_PROMPT_FILE"
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
      # Persist the result where the registry already points: auto-promoted
      # jobs never wrote output.md, so「job output <id>」answered "No output
      # found" even for completed work (j-1786098762, 2026-08-07 — the user
      # was told the result would come back, then had nowhere to read it).
      printf '%s\n' "$reply" > "$JOBS_DIR/$_promoted_job/output.md" 2>>"$LOG_FILE" || true
      JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" finish "$_promoted_job" completed \
        2>>"$LOG_FILE" || true
      python3 -m core.conversation_context queue-pending \
        --path "$JOBS_DIR/pending_merge.jsonl" --conv-key "$conv_key" \
        --context-key "$logical_context_key" --job-id "$_promoted_job" \
        --timestamp "$(date '+%Y-%m-%d %H:%M')" --summary "${reply:0:1500}" \
        >>"$LOG_FILE" 2>&1 || true
      log_info "[$session_id] Promoted job $_promoted_job completed (${#reply} chars)"
    else
      printf '（任务失败：模型未产出结果 @ %s）\n' "$(date '+%Y-%m-%d %H:%M')" \
        > "$JOBS_DIR/$_promoted_job/output.md" 2>>"$LOG_FILE" || true
      JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" finish "$_promoted_job" failed \
        2>>"$LOG_FILE" || true
      log_warn "[$session_id] Promoted job $_promoted_job finished empty/error"
    fi
  fi

  if [ -z "$reply" ]; then
    log_warn "[$session_id] Final empty/error answer from model chain (${#answer} chars after ${_attempt:-?} attempts)"
    if [ -n "$answer" ]; then
      log_warn "[$session_id] Suppressed content: ${answer:0:500}"
    fi
    # Tell user exactly what happened — not a vague "try again"
    if [ "${#answer}" -eq 0 ]; then
      if [ "${_no_healthy_provider:-0}" -eq 1 ]; then
        lark_reply_text "$message_id" \
          "当前已配置的模型通道都在恢复中，这次请求没有执行。Jarvis 会自动探测恢复，不需要反复重试。" >/dev/null
      elif [ "${_codex_cancelled:-0}" -eq 1 ]; then
        log_info "[$session_id] Cancelled Codex turn ended — staying silent"
      elif [ "${_codex_uncertain:-0}" -eq 1 ]; then
        lark_reply_text "$message_id" \
          "Codex 执行被中断，是否已经完成无法确认。为避免重复操作，我没有自动换模型重跑；可以先让我核对结果，再决定是否继续。" >/dev/null
      elif [ "${_exit_code:-0}" -eq 143 ] && [ "$_watchdog_killed" -eq 1 ] && [ -n "$_promoted_job" ]; then
        # Promoted job hit the 6000s ceiling. 「继续」 would land in the NEW
        # (rotated) session and not resume this work — say so honestly.
        lark_reply_text "$message_id" \
          "这件后台工作运行太久，已经安全停下。进度已保留；回复「继续这件事」，我会先核对已有结果再接着做。" >/dev/null
      elif [ "${_exit_code:-0}" -eq 143 ] && [ "$_watchdog_killed" -eq 1 ]; then
        # Genuine 6000s watchdog timeout: the task really ran long. Resuming
        # with 「继续」 is the right recovery.
        lark_reply_text "$message_id" \
          "这件事运行太久，已经安全停下，进度还在。回复「继续」，我会从已有结果接着处理。" >/dev/null
      elif [ "${_exit_code:-0}" -eq 143 ]; then
        # 143 WITHOUT the watchdog marker = a restart / external SIGTERM killed
        # the in-flight Claude. Telling the user to say 「继续」 here is exactly
        # the bug that produced the restart-loop nag: 「继续」 re-runs whatever
        # was interrupted (often the very restart). Stay silent — the post-restart
        # startup path already notifies "重启中断了，请重发" from the message queue.
        # EXCEPT for a promoted job: the promotion message promised 「做完我会
        # 把结果发回来」, so ending in silence is a broken promise, not calm.
        if [ -n "$_promoted_job" ]; then
          lark_reply_text "$message_id" \
            "这件后台工作被外部中断，没有产出结果。回复「继续这件事」，我会先核对已有进度再接着做。" >/dev/null
          log_warn "[$session_id] exit=143 without watchdog marker — promoted job $_promoted_job, receipt sent"
        else
          log_warn "[$session_id] exit=143 without watchdog marker — restart/external kill, staying silent"
        fi
      else
        # Transient empty response (API blip). We already retried silently up to
        # 4x with backoff above. Nagging "请稍后重试" just forces the user to tell
        # us to retry by hand — exactly the boring loop they asked us to remove.
        # Stay silent: the reaction is cleared above so the turn visibly ends,
        # and the user can resend if they were actually waiting on a reply.
        # EXCEPT for a promoted job (no retries happen after promotion — the
        # first empty answer lands here): the user was told a result would
        # come back, and a job that ends in silence leaves them waiting on
        # nothing (j-1786098762 class, 2026-08-07).
        if [ -n "$_promoted_job" ]; then
          lark_reply_text "$message_id" \
            "这件后台工作结束了，但没能产出可交付的结果。回复「继续这件事」，我会先核对已有进度再重试。" >/dev/null
          log_warn "[$session_id] Promoted job $_promoted_job ended empty — receipt sent"
        else
          log_warn "[$session_id] Empty after $_attempt attempts — staying silent (user opted out of the retry nag)"
        fi
      fi
    else
      # looks_like_error suppresses provider/auth/CLI failures; it is not a
      # content-safety classifier. Calling this a "安全过滤器" sent Pascal
      # debugging the wrong subsystem while the real fault was an exhausted
      # provider chain.
      if [ "${_codex_tried:-0}" -eq 1 ] && [ "${_openai_tried:-0}" -eq 1 ]; then
        lark_reply_text "$message_id" \
          "Claude、Codex 接力和 GPT 兜底都未能完成这次请求。本次操作没有执行成功，具体故障已记录。" >/dev/null
      elif [ "${_codex_tried:-0}" -eq 1 ]; then
        lark_reply_text "$message_id" \
          "Claude 和 Codex 接力都未能完成这次请求。本次操作没有执行成功，具体故障已记录。" >/dev/null
      else
        lark_reply_text "$message_id" \
          "模型通道返回了错误信息，本次操作没有执行成功，具体故障已记录。" >/dev/null
      fi
    fi
    _finish_message_handler
    return
  fi

  # Reply footer: only when NOT served by primary, and in plain Chinese —
  # the old English "Model: Claude backup opus" on EVERY reply was jargon in
  # Pascal's chat (2026-07-07; feedback-no-jargon-dashboards). Silence on
  # primary = normal operation needs no caption.
  # Never in groups: provider status is the owner's internal ops detail —
  # "（备用通道）" leaking into a group reply confused the first live test
  # (2026-07-14 16:04) and tells outsiders about the owner's infra state.
  local _model_footer=""
  if [ "$is_group" -ne 1 ]; then
    if [ "$_answer_provider" = "Claude backup" ]; then
      _model_footer="（备用通道）"
    elif [ "$_answer_provider" = "Claude backup2" ]; then
      _model_footer="（备用通道 2）"
    elif [ "$_answer_provider" = "Codex" ]; then
      _model_footer="（Codex 接手）"
    elif [ "$_answer_provider" = "GPT fallback" ]; then
      _model_footer="（GPT 兜底）"
    elif [ -n "$_answer_model" ] && [ "$_answer_model" != "$MAIN_MODEL" ]; then
      # REQ-111: within-provider degrade (REQ-77 opus→sonnet→haiku) was the
      # one silent switch left — 7/14-16 he could not tell which replies came
      # from a degraded model. Same rule as above: silence = primary+opus.
      _model_footer="（临时 ${_answer_model} 代答）"
    fi
  fi

  # REQ-102: group sessions must NOT write to ANY owner-private store:
  # - [SAVE_LATER:] → watchlater (red-team: marker teachable via injection)
  # - pending_merge → bg job output keyed to group conv_key (red-team: memory
  #   leak via bg output merge path)
  # Strip these markers BEFORE any processing, so they never execute.
  if [ "$is_group" -eq 1 ]; then
    reply=$(printf '%s' "$reply" | sed 's/\[SAVE_LATER:[^]]*\]//g')
  fi

  # ── Process [ACTION:...] markers (LLM-driven action system) ──
  reply=$(process_actions "$reply" "$conv_key" "$message_id" "$allow_actions" \
    "$logical_context_key" "$matter_id" "$session_id")

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

  # Send the real reply. Unified handler cleanup removes the Typing reaction
  # after it first unregisters this process identity.
  if ! JARVIS_DELIVERY_PROVIDER="${_answer_provider:-}" \
       JARVIS_DELIVERY_MODEL="${_answer_model:-$MAIN_MODEL}" \
       delivery_reply_reliable "$message_id" "$reply"; then
    log_err "[$session_id] Reply not yet delivery-confirmed — retained in the durable queue"
  else
    # Write-claim audit (REQ-88, SHADOW): if the reply claims "已记录/已写入",
    # reconcile against actual write-surface mtimes and append the verdict to
    # data/write_claim_audit.jsonl. Strictly log-only — never messages, never
    # writes on Jarvis's behalf. Backgrounded + fully guarded, so it can never
    # delay the reply path (same pattern as the journal_capture hook).
    ( JV_REPLY="$reply" JARVIS_DIR="$JARVIS_DIR" MEMORY_DIR="$MEMORY_DIR" \
      python3 "$JARVIS_DIR/tasks/write_claim_audit.py" >>"$LOG_FILE" 2>&1 & )
    # Keep the Matter timeline on the same successful-delivery boundary as
    # the user-visible reply. A failed Lark send is not recorded as delivered.
    ( JV_CONV_KEY="$conv_key" JV_REPLY="$reply" JV_MSG_ID="$message_id" \
      JV_MODEL="${_answer_model:-$MAIN_MODEL}" JARVIS_DIR="$JARVIS_DIR" \
      JV_CONTEXT_KEY="$logical_context_key" JV_MATTER_ID="$matter_id" \
      python3 -m core.matter_bridge --conv-key "$conv_key" \
        --record-role assistant --content "$reply" --message-id "$message_id" \
        --model "${_answer_model:-$MAIN_MODEL}" \
        --provider "${_answer_provider:-Claude primary}" \
        --session-id "$session_id" --context-key "$logical_context_key" \
        --matter-id "$matter_id" >>"$LOG_FILE" 2>&1 & )
    resolve_memorial_thread_after_reply "$conv_key" "$reply"
  fi
  _finish_message_handler
}

# A successful reply in a memorial thread is itself a completed handoff.
# Close the original card only at a confirmed-delivery boundary; failed
# provider or Lark attempts leave it pending and retryable.
resolve_memorial_thread_after_reply() {
  local conv_key="$1" reply="$2"
  case "$conv_key" in
    memorial:*)
      ( JV_MEM_CONV_KEY="$conv_key" JV_MEM_REPLY="$reply" \
        JARVIS_DIR="$JARVIS_DIR" python3 -m core.memorial resolve-thread \
        >>"$LOG_FILE" 2>&1 & )
      ;;
  esac
}

# ── Background Job Runner ────────────────────────────────────────────
# Runs a Claude task in an independent session, notifies on completion.
run_background_job() {
  local job_id="$1" conv_key="$2" content="$3" message_id="$4"
  local job_context_key="conversation:$conv_key" source_session_id=""
  _job_scope=$(JV_JOBS_DIR="$JOBS_DIR" JV_JOB_ID="$job_id" python3 -c "
import json, os
path = os.path.join(os.environ['JV_JOBS_DIR'], 'registry.json')
try:
    job = json.load(open(path)).get(os.environ['JV_JOB_ID'], {})
    print(job.get('context_key', ''))
    print(job.get('source_session_id', ''))
except Exception:
    print('')
    print('')
" 2>>"$LOG_FILE" || true)
  job_context_key=$(printf '%s\n' "$_job_scope" | sed -n '1p')
  source_session_id=$(printf '%s\n' "$_job_scope" | sed -n '2p')
  [ -z "$job_context_key" ] && job_context_key="conversation:$conv_key"
  # claude CLI requires --session-id to be a valid UUID; the old "bg-<jobid>"
  # scheme is rejected ("Invalid session ID. Must be a valid UUID.").
  local bg_session_id
  bg_session_id="$(uuidgen 2>/dev/null | tr 'A-Z' 'a-z')"
  [ -z "$bg_session_id" ] && bg_session_id="$(python3 -c 'import uuid;print(uuid.uuid4())')"
  # Persist the actual provider session before Claude starts. Cross-session
  # continuity reads this registry to exclude Jarvis-owned background work;
  # without the real UUID a fork could be mistaken for an interactive session.
  local _session_marked
  _session_marked=$(JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" \
    set-session "$job_id" "$bg_session_id" 2>>"$LOG_FILE" || true)
  if [ "$_session_marked" != "updated" ]; then
    log_warn "[bg:$job_id] Could not register provider session — refusing untracked launch"
    JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" finish "$job_id" failed \
      2>>"$LOG_FILE" || true
    return
  fi
  local output_file="$JOBS_DIR/${job_id}/output.md"
  local log_file_job="$JOBS_DIR/${job_id}/log.txt"

  # Background jobs use the shared auxiliary router, which follows the sticky
  # gate without taking a primary-probe slot and can continue through Backup 1,
  # Backup 2, and the tool-capable GPT fallback.
  local _bg_gate="primary"
  local _bg_mem_budget=""
  _bg_gate=$(python3 -m core.model_fallback --gate no-probe 2>/dev/null || echo primary)
  if [ "$_bg_gate" = "backup" ]; then
    _bg_mem_budget="${BACKUP_MAX_MEMORY_CHARS:-40000}"
    log_info "[bg:$job_id] Provider gate: primary unavailable — following fallback chain"
  fi

  # Build a minimal system prompt for the background job
  local memory now_ts sys_prompt
  memory=$(load_memory "$_bg_mem_budget")
  now_ts=$(date '+%Y-%m-%d %H:%M %A')

  sys_prompt="You are running as a background job. Complete the task thoroughly.
When done, provide a clear summary of results.
Current time: $now_ts

$memory"
  local sys_prompt_file="$JOBS_DIR/${job_id}/system_prompt.txt"
  local provider_file="$JOBS_DIR/${job_id}/provider.json"
  rm -f "$provider_file"
  printf '%s' "$sys_prompt" > "$sys_prompt_file"

  # Inherit conversation context (REQ-16 MVP-1): fork from the conversation's
  # active session if it has a transcript — the job sees the full dialog
  # history without polluting the main session, and reuses the prompt cache.
  # Falls back to a fresh session for conversations with no history.
  local _main_sid="$source_session_id"

  # set -m: give the job its OWN process group (REQ-38). Without it the
  # subshell shares bot.sh's group and cancel_job's killpg SIGTERMed the
  # ENTIRE bot — user-facing "cancel <job>" restarted the whole product.
  # With its own group, killpg cleanly reaps subshell + with_timeout + claude.
  set -m
  if [ -n "$_main_sid" ] && [ -f "$CLAUDE_PROJECT_DIR/${_main_sid}.jsonl" ]; then
    log_info "[bg:$job_id] Forking from session $_main_sid"
    (cd "$WORK_DIR" && printf '%s' "$content" | python3 -m core.aux_model \
      --allow-tools --timeout 6000 --model "$MAIN_MODEL" \
      --system-prompt-file "$sys_prompt_file" \
      --consume-system-prompt-file \
      --metadata-file "$provider_file" \
      --managed-job-id "$job_id" --jobs-dir "$JOBS_DIR" \
      --resume "$_main_sid" --fork-session --session-id "$bg_session_id" \
      2>>"$log_file_job" > "$output_file") &
  else
    (cd "$WORK_DIR" && printf '%s' "$content" | python3 -m core.aux_model \
      --allow-tools --timeout 6000 --model "$MAIN_MODEL" \
      --system-prompt-file "$sys_prompt_file" \
      --consume-system-prompt-file \
      --metadata-file "$provider_file" \
      --managed-job-id "$job_id" --jobs-dir "$JOBS_DIR" \
      --session-id "$bg_session_id" \
      2>>"$log_file_job" > "$output_file") &
  fi
  local _bg_pid=$!
  set +m

  # Record PID in registry
  JV_JOBS_DIR="$JOBS_DIR" python3 "$JARVIS_DIR/core/jobs.py" set-pid "$job_id" "$_bg_pid" \
    2>>"$LOG_FILE" || log_warn "[bg:$job_id] Failed to register PID"

  # Wait for completion
  local exit_code=0
  wait "$_bg_pid" 2>/dev/null || exit_code=$?
  rm -f "$sys_prompt_file"

  # Read output
  local output=""
  [ -f "$output_file" ] && output=$(cat "$output_file" 2>/dev/null)

  # Determine status
  local status="completed"
  if [ "$exit_code" -ne 0 ] || [ -z "$output" ] || looks_like_error "$output"; then
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

  local _bg_provider=""
  if [ -f "$provider_file" ]; then
    _bg_provider=$(python3 -c "import json; d=json.load(open('$provider_file')); print(f\"{d.get('provider','')} {d.get('model','')}\".strip())" 2>/dev/null || true)
  fi
  log_info "[bg:$job_id] Finished with status=$status provider=${_bg_provider:-none} (${#output} chars)"

  # Queue the result for context merge (REQ-16): the conversation's next
  # message gets this summary prepended, so the dialog "knows" what the job
  # found instead of the result living only in a notification card.
  if [ "$status" = "completed" ]; then
    python3 -m core.conversation_context queue-pending \
      --path "$JOBS_DIR/pending_merge.jsonl" --conv-key "$conv_key" \
      --context-key "$job_context_key" --job-id "$job_id" \
      --timestamp "$(date '+%Y-%m-%d %H:%M')" --summary "${output:0:1500}" \
      >>"$LOG_FILE" 2>&1 || true
  fi

  # Notify user via card
  local card_body card_json
  if [ "$status" = "completed" ]; then
    # Keep the Lark result readable; the full result remains in the private
    # job ledger without making Pascal learn job commands.
    local summary
    if [ ${#output} -gt 3000 ]; then
      summary="${output:0:3000}

内容较长，我保留了完整结果。需要时直接问我继续展开。"
    else
      summary="$output"
    fi
    card_body="这件事做完了。

$summary"
  else
    card_body="这件事这次没跑完。我保留了现场，但没有自动重跑，避免重复执行。直接回复“继续”，我会从这里接上。"
  fi
  card_json=$(JV_BODY="$card_body" python3 -c "
import os, sys; sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.card import build_card
print(build_card('后台工作结果', os.environ['JV_BODY']))
" 2>/dev/null) || card_json=""
  if [ -n "$card_json" ]; then
    delivery_card_reliable "$card_json" || send_to_lark "$card_body"
  else
    send_to_lark "$card_body"
  fi
}

# Cleanup on exit
_CLEANED_UP=0
cleanup() {
  [ "$_CLEANED_UP" -eq 1 ] && return
  _CLEANED_UP=1
  log_info "Shutting down..."
  # Save in-flight message sessions so next startup can notify user
  _queue_file="$JARVIS_DIR/.message_queue"
  rm -f "$_queue_file"
  for _lock in "$JARVIS_DIR"/.session_lock_*; do
    [ -f "$_lock" ] || continue
    basename "$_lock" | sed 's/^\.session_lock_//' >> "$_queue_file"
  done
  # Stop every live message handler before the bot exits.  Killing only the
  # top-level bot reparents provider/tool children and lets them keep acting
  # after a restart; dispatch markers are the authoritative handler registry.
  for _dispatch_marker in "$JARVIS_DIR"/.dispatch_*; do
    [ -f "$_dispatch_marker" ] || continue
    terminate_registered_group "$_dispatch_marker" "$$" || true
    rm -f -- "$_dispatch_marker"
  done
  # Only remove the pidfile if it is still OURS. During a guardian/daemon
  # restart the new bot.sh writes its pid before the old instance finishes
  # shutting down — an unconditional rm here deletes the NEW instance's
  # pidfile (7/7 incident: left .bot.pid missing, health checks went blind
  # and the double-start guard was disarmed).
  if [ "$(awk '{print $1}' "$PIDFILE" 2>/dev/null)" = "$$" ]; then
    rm -f "$PIDFILE"
  fi
  [ -n "$ADMIN_PID" ] && kill "$ADMIN_PID" 2>/dev/null || true
  [ -n "$STREAM_PID" ] && kill "$STREAM_PID" 2>/dev/null || true
  [ -n "$WATCHDOG_PID" ] && kill "$WATCHDOG_PID" 2>/dev/null || true
  kill "$HEARTBEAT_PID" 2>/dev/null || true
  # $HEARTBEAT_PID can be STALE: the watchdog respawns heartbeat inside its
  # own subshell, so its updates never reach this shell's variable (7/6
  # incident: cleanup killed a long-dead pid, orphaning the real heartbeat,
  # which then held the singleton flock through the next bot for 2.5 days).
  # Sweep by exact process identity, mirroring the startup sweep. Worst case
  # during an overlapping restart this kills the NEW bot's just-spawned
  # heartbeat — its watchdog respawns it within 30s, which beats an orphan.
  ps -eo pid,comm,args | awk '$4 == "-m" && $5 == "core.heartbeat_loop" {print $1}' \
    | xargs kill 2>/dev/null || true
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
trap cleanup EXIT
trap 'cleanup; exit 0' INT TERM

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
        # impossible (history was simply destroyed). Keep 8 generations
        # (REQ-80: 3 covered only ~2.5 days of history).
        for _gen in 7 6 5 4 3 2 1; do
          mv -f "$LOG_FILE.$_gen" "$LOG_FILE.$((_gen + 1))" 2>/dev/null || true
        done
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
    if [ "${JARVIS_ALLOW_UNRELEASED_RUNTIME:-false}" != "true" ] \
        && ! runtime_source_unchanged; then
      log_warn "[watchdog] Runtime source changed after startup; child respawns are blocked until governed deploy"
      sleep 30
      continue
    fi
    # Re-assert the pidfile (self-heal): an overlapping old instance's cleanup
    # can delete OUR pidfile during a guardian restart (pre-7/7 code did an
    # unconditional rm). Without it the daemon's health checks go pgrep-blind
    # and the double-start guard is disarmed. $$ is the main bot pid here —
    # bash keeps $$ stable inside background functions (BASHPID differs).
    if [ "$(awk '{print $1}' "$PIDFILE" 2>/dev/null)" != "$$" ]; then
      log_warn "[watchdog] Pidfile missing or foreign — re-asserting ($$)"
      echo "$$ $_BOOT_TS" > "$PIDFILE"
    fi
    if ! kill -0 "$HEARTBEAT_PID" 2>/dev/null; then
      # Adopt a surviving singleton before declaring death (7/7 incident: an
      # orphaned heartbeat from a previous bot held the flock for 2.5 days;
      # every respawn exited on the lock instantly, so the watchdog logged
      # 107 phantom "crashes" while the real heartbeat beat happily).
      # ps+awk, NOT pgrep -f: a substring match would "adopt" any shell that
      # merely mentions the module name, leaving the real heartbeat dead.
      local _survivor
      _survivor=$(ps -eo pid,comm,args | awk '$4 == "-m" && $5 == "core.heartbeat_loop" {print $1}' | head -1)
      if [ -n "$_survivor" ]; then
        log_warn "[watchdog] Heartbeat PID $HEARTBEAT_PID gone but PID $_survivor holds the singleton — adopting it"
        HEARTBEAT_PID=$_survivor
        _fails=0
      else
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
            | _append_elog
          printf '%s' "${_read_ids:-[]}" \
            | python3 -m core.delivery confirm-read --stdin \
              >/dev/null 2>>"$LOG_FILE" || true
          continue ;;
        im.message.reaction.created_v1)
          # Ignore the bot's own reactions (e.g. the "Typing" indicator)
          _re_op=$(echo "$line" | jq -r '.event.operator_type // empty' 2>/dev/null)
          if [ "$_re_op" != "app" ]; then
            _re_mid=$(echo "$line" | jq -r '.event.message_id // empty' 2>/dev/null)
            _re_emoji=$(echo "$line" | jq -r '.event.reaction_type.emoji_type // empty' 2>/dev/null)
            # REQ-102: reaction-based writes (engagement log + watchlater) are
            # owner-only. A group member's emoji would write to private stores
            # and send a confirmation reply visible to all (red-team catch).
            _re_operator_id=$(echo "$line" | jq -r '.event.user_id.open_id // empty' 2>/dev/null)
            if [ -n "$_re_operator_id" ] && [ "$_re_operator_id" != "$USER_ID" ]; then
              log_info "[engagement] skipped non-owner reaction (${_re_operator_id: -6})"
              continue
            fi
            jq -cn --arg ts "$(date '+%Y-%m-%d %H:%M')" --arg mid "$_re_mid" \
              --arg emoji "$_re_emoji" --argjson epoch "$(date +%s)" \
              '{ts:$ts,type:"reaction",message_id:$mid,emoji:$emoji,epoch:$epoch}' \
              | _append_elog
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
            | _append_elog
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
      _root_id=$(echo "$line" | jq -r '.root_id // .event.message.root_id // empty' 2>/dev/null)

      # Log every received event for debugging (even if we skip it)
      if [ -n "$message_id" ]; then
        log_info "Event: msg_type=${msg_type:-text} content_len=${#content} mid=${message_id} chat_type=${chat_type} content_head=${content:0:80}"
      fi

      [ -z "$content" ] || [ -z "$message_id" ] && continue
      if [ "$chat_type" = "p2p" ] && [ -z "$sender_id" ]; then
        log_warn "P2P message missing sender_id — refusing private dispatch"
        continue
      fi
      _owner_p2p=0
      if [ "$chat_type" = "p2p" ] && [ "$sender_id" = "$USER_ID" ]; then
        _owner_p2p=1
      fi

      # ── Dedup: skip if message_id seen recently (Lark replays on late ACK) ──
      _dedup_file="/tmp/jarvis-msg-dedup"
      if [ -f "$_dedup_file" ] && grep -qFx "$message_id" "$_dedup_file" 2>/dev/null; then
        log_info "Duplicate message skipped: $message_id"
        continue
      fi
      # Ring buffer: keep last 20 message_ids
      { echo "$message_id"; head -19 "$_dedup_file" 2>/dev/null; } > "${_dedup_file}.tmp" \
        && mv "${_dedup_file}.tmp" "$_dedup_file"

      # Journal capture (PRD P1, 每日复盘 check-in): if this message quotes the
      # daily-reflect card, save Pascal's OWN words ("我怎么看一些事") into his
      # private 《Jarvis 日志》. For DIRECT (non-quote) messages the script is
      # REQ-86 SHADOW ONLY: it just logs an attribution candidate to
      # data/journal_capture_shadow.jsonl and never writes the journal.
      # Backgrounded so it never delays the reply; fully guarded.
      ( JV_PARENT="$_parent_id" JV_REPLY="$content" JV_CHAT_TYPE="$chat_type" \
        JV_MSG_TYPE="${msg_type:-text}" JV_SENDER="$sender_id" JV_USER_ID="$USER_ID" \
        JARVIS_DIR="$JARVIS_DIR" \
        python3 "$JARVIS_DIR/tasks/journal_capture.py" >>"$LOG_FILE" 2>&1 & )

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
    from core.intent_closure import record_closure
    from core.intent_lifecycle import get_intent
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
        _mentioned=0
        echo "$mentions_raw" | grep -q "$APP_ID" 2>/dev/null && _mentioned=1
        # Lark mentions reference a bot by its open_id (ou_...), not APP_ID.
        if [ "$_mentioned" -eq 0 ] && [ -n "${BOT_OPEN_ID:-}" ]; then
          echo "$mentions_raw" | grep -q "$BOT_OPEN_ID" 2>/dev/null && _mentioned=1
        fi
        if [ "$_mentioned" -eq 0 ]; then
          log_info "Group message without @mention — ignoring (mentions_head=${mentions_raw:0:120})"
          continue
        fi
      fi

      # Resolve @mention placeholders (group): Lark text carries opaque
      # "@_user_N" tokens; unresolved, the bot can't tell IT was the one
      # being addressed (first live test 2026-07-14 16:04: it read
      # "@Kalpas hi" as the owner greeting a stranger and bowed out).
      if [ "$chat_type" != "p2p" ] && [ -n "$content" ]; then
        content=$(JV_LINE="$line" JV_CONTENT="$content" JV_BOT_OID="${BOT_OPEN_ID:-}" python3 -c "
import json, os
content = os.environ['JV_CONTENT']
try:
    ev = json.loads(os.environ['JV_LINE'])
    mentions = (ev.get('mentions')
                or (ev.get('event') or {}).get('message', {}).get('mentions')) or []
    bot_oid = os.environ.get('JV_BOT_OID', '')
    # Longest keys first: replacing '@_user_1' before '@_user_10' would
    # corrupt the latter (prefix collision).
    for m in sorted(mentions, key=lambda m: len(m.get('key') or ''), reverse=True):
        key = m.get('key') or ''
        if not key:
            continue
        oid = ((m.get('id') or {}).get('open_id')) or ''
        name = m.get('name') or '某人'
        label = '@你' if (bot_oid and oid == bot_oid) else f'@{name}'
        content = content.replace(key, label)
except Exception:
    pass
print(content)
" 2>>"$LOG_FILE" || printf '%s' "$content")
      fi

      # Determine conv_key early (needed by most commands)
      if [ "$chat_type" = "p2p" ]; then
        conv_key="$sender_id"
      else
        conv_key="$chat_id"
      fi

      # ── 奏折专属对话 (REQ-118): a reply whose thread root (or parent)
      # is a delivered memorial card gets its own conv_key — and therefore
      # its own session — scoped to that one matter. p2p ONLY: in a group
      # the reply is publicly visible while the per-card session carries the
      # owner's private memory, and prompt.py would take the group path
      # where the memorial context is never injected (red-team 7/21).
      _mem_id=""
      _mem_title=""
      if { [ -n "$_root_id" ] && [ "$_root_id" != "null" ]; } || \
         { [ -n "$_parent_id" ] && [ "$_parent_id" != "null" ]; }; then
        if [ "$_owner_p2p" -eq 1 ]; then
          _mem_route=$(JV_ROOT="$_root_id" JV_PARENT="$_parent_id" \
            JARVIS_DIR="$JARVIS_DIR" python3 -m core.memorial_thread route \
            2>>"$LOG_FILE")
          if [ -n "$_mem_route" ]; then
            _mem_id="${_mem_route%%	*}"
            _mem_title="${_mem_route#*	}"
            conv_key="memorial:${_mem_id}"
            log_info "Memorial thread routed: mid=$_mem_id title=${_mem_title:0:40}"
          fi
        fi
      fi

      # Handle special commands (these run inline, NOT dispatched to background)
      # ONLY stop/cancel bypass Claude — everything else goes through LLM + action markers
      content_lower=$(echo "$content" | tr '[:upper:]' '[:lower:]')

      # REQ-102: inline commands (broadcast confirm, stop/cancel) are
      # owner-only in groups — a non-owner's 「发」/「stop」 is just chat and
      # falls through to the LLM path.
      _inline_cmd_ok=0
      if [ -n "$sender_id" ] && [ "$sender_id" = "$USER_ID" ]; then
        _inline_cmd_ok=1
      fi

      # Explicit Matter commands are deterministic and do not spend a model
      # turn. Ordinary conversation remains untouched.
      if [ "$_inline_cmd_ok" -eq 1 ]; then
        _matter_transition=$(JV_CONTENT="$content" python3 -c "
import os
from core.matter_bridge import command_would_transition
print('true' if command_would_transition(os.environ.get('JV_CONTENT', '')) else 'false')
" 2>>"$LOG_FILE" || echo false)
        if [ "$_matter_transition" = "true" ]; then
          _current_sid=$(JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" python3 -c "
import json, os
try:
    print(json.load(open(os.environ['JV_TRACKER'])).get(os.environ['JV_KEY'], {}).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
          _current_lock="$JARVIS_DIR/.session_lock_${_current_sid}"
          _dispatch_active=0
          _conv_dispatch_key=$(printf '%s' "$conv_key" | shasum -a 256 | cut -c1-16)
          if find "$JARVIS_DIR" -maxdepth 1 \
               -name ".dispatch_conv_${_conv_dispatch_key}_*" -print -quit 2>/dev/null \
               | grep -q .; then
            _dispatch_active=1
          fi
          if { [ -n "$_current_sid" ] && [ -f "$_current_lock" ]; } \
             || [ "$_dispatch_active" -eq 1 ]; then
            delivery_reply_reliable "$message_id" \
              "当前回复还在运行。请先发停止，再新开、切换或重置会话。" || true
            continue
          fi
        fi
        _matter_cmd=$(run_matter_command \
          "$content" "$conv_key" "${chat_id:-$sender_id}" "$chat_type")
        if [ "$(echo "$_matter_cmd" | jq -r '.handled // false' 2>/dev/null)" = "true" ]; then
          _matter_reply=$(echo "$_matter_cmd" | jq -r '.reply // "事项命令已处理"' 2>/dev/null)
          if delivery_reply_reliable "$message_id" "$_matter_reply"; then
            resolve_memorial_thread_after_reply "$conv_key" "$_matter_reply"
          fi
          continue
        fi
      fi

      # A clipped memorial explicitly promises that these exact replies will
      # deliver the rest. Keep this deterministic and owner-only; group cards
      # promise the same continuation, so the owner may fulfill it there too.
      # The lookup keys cover direct chat ids and per-card thread routing.
      if [ "$_inline_cmd_ok" -eq 1 ] && \
         { [ "$content" = "继续发" ] || [ "$content" = "继续发送" ] || \
           [ "$content" = "发剩下的" ]; }; then
        _memorial_continue=$(python3 -m core.memorial continue \
          --conv-key "$conv_key" --lookup-key "$chat_id" \
          --memorial-id "${_mem_id:-}" \
          2>>"$LOG_FILE" || echo '{"handled":false}')
        if [ "$(echo "$_memorial_continue" | jq -r '.handled // false' 2>/dev/null)" = "true" ]; then
          _continue_reply=$(echo "$_memorial_continue" | jq -r '.reply // empty' 2>/dev/null)
          if [ "$(echo "$_memorial_continue" | jq -r '.awaiting_opener // false' 2>/dev/null)" = "true" ]; then
            delivery_reply_reliable "$message_id" "$_continue_reply" || true
          elif [ -n "$_continue_reply" ] && \
             delivery_reply_reliable "$message_id" "$_continue_reply"; then
            _continue_mid=$(echo "$_memorial_continue" | jq -r '.memorial_id // empty')
            _continue_state_key=$(echo "$_memorial_continue" | jq -r '.state_conv_key // empty')
            _continue_expected=$(echo "$_memorial_continue" | jq -r '.expected_offset // -1')
            _continue_next=$(echo "$_memorial_continue" | jq -r '.next_offset // -1')
            if python3 -m core.memorial continue-commit \
                 --conv-key "$conv_key" --state-conv-key "$_continue_state_key" \
                 --memorial-id "$_continue_mid" \
                 --expected-offset "$_continue_expected" \
                 --next-offset "$_continue_next" 2>>"$LOG_FILE" >/dev/null; then
              log_info "Memorial continuation delivered and committed: conv_key=$conv_key"
              resolve_memorial_thread_after_reply "$conv_key" "$_continue_reply"
            else
              log_warn "Memorial continuation delivered but commit failed: conv_key=$conv_key"
            fi
          else
            log_warn "Memorial continuation not delivered; offset retained: conv_key=$conv_key"
          fi
          continue
        fi
      fi

      # "发" — confirm pending EigenFlux broadcast; "不发" — cancel it
      if [ "$_inline_cmd_ok" -eq 1 ] && { [ "$content" = "发" ] || [ "$content" = "不发" ]; }; then
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
      if [ "$_inline_cmd_ok" -eq 1 ] && { [ "$content_lower" = "stop" ] || [ "$content_lower" = "cancel" ]; }; then
        _stop_sid=$(JV_TRACKER="$SESSION_TRACKER" JV_KEY="$conv_key" python3 -c "
import json, os
try:
    print(json.load(open(os.environ['JV_TRACKER'])).get(os.environ['JV_KEY'], {}).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
        _stop_lock="$JARVIS_DIR/.session_lock_${_stop_sid}"
        _stopped_any=0
        _conv_dispatch_key=$(printf '%s' "$conv_key" | shasum -a 256 | cut -c1-16)
        if [ -f "$_stop_lock" ]; then
          _stop_identity=""
          _stop_safe=0
          # A lock can only authorize a kill when its owner token and provider
          # ancestry both lead back to a live, bot-owned marker for this exact
          # conversation.  PID/start validation alone does not prove ownership.
          for _owner_marker in "$JARVIS_DIR"/".dispatch_conv_${_conv_dispatch_key}_"*; do
            [ -f "$_owner_marker" ] || continue
            _owner_record=$(dispatch_marker_record "$_owner_marker" 2>/dev/null || true)
            IFS=$'\t' read -r _owner_pid _owner_start <<< "$_owner_record"
            if process_group_is_owned "$_owner_pid" "$_owner_start" "$$"; then
              _stop_identity=$(session_lock_identity_for_handler \
                "$_stop_lock" "$_owner_pid" "$_owner_start" 2>/dev/null || true)
              [ -n "$_stop_identity" ] && { _stop_safe=1; break; }
            fi
          done
          IFS=$'\t' read -r _stop_pid _stop_start <<< "$_stop_identity"
          if [ "$_stop_safe" -eq 1 ]; then
            # The wrapper handles TERM by reaping its detached model/tool
            # sessions. The dispatch marker pass below applies the hard group
            # fallback if that graceful cleanup does not complete.
            kill -TERM "$_stop_pid" 2>/dev/null || true
            log_info "[$_stop_sid] Killed by user (PID $_stop_pid)"
            _stopped_any=1
          else
            log_warn "[$_stop_sid] Stale/foreign PID $_stop_pid in lock — refusing to kill"
          fi
          rm -f "$_stop_lock"
        fi
        # A handler can be queued before it owns a provider lock.  Its
        # conversation-scoped marker carries the subshell PID so stop remains a
        # truthful escape hatch before, during, and after lock acquisition.
        for _queued_marker in "$JARVIS_DIR"/".dispatch_conv_${_conv_dispatch_key}_"*; do
          [ -f "$_queued_marker" ] || continue
          _queued_pid=$(dispatch_marker_pid "$_queued_marker" 2>/dev/null || true)
          if terminate_registered_group "$_queued_marker" "$$"; then
            _stopped_any=1
            log_info "Queued handler killed by user (PID $_queued_pid)"
          elif [ "$_queued_pid" = "$$" ]; then
            log_warn "Refusing queued marker that points at the bot PID"
          fi
          rm -f "$_queued_marker"
        done
        if [ "$_stopped_any" -eq 1 ]; then
          lark_reply_text "$message_id" "已停止。现在可以切换或重置会话。" >/dev/null
        else
          lark_reply_text "$message_id" "当前没有运行或排队中的回复。" >/dev/null
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

      # Provider-aware session sizing: backup relay has a much smaller context
      # window than the primary 1M channel — rotate sessions earlier to prevent
      # "prompt is too long" / "autocompact is thrashing" errors.
      _session_max="$MAX_SESSION_SIZE"
      _dispatch_gate=$(python3 -m core.model_fallback --gate 2>/dev/null || echo primary)
      if [ "$_dispatch_gate" != "primary" ] \
        && [ "${CLAUDE_BACKUP_ENABLED:-true}" = "true" ] \
        && [ -n "${CLAUDE_BACKUP_AUTH_TOKEN:-}" ]; then
        _session_max="${BACKUP_MAX_SESSION_SIZE:-100000}"
      fi

      # macOS ships Bash 3.2, where expanding an empty array under `set -u`
      # raises "unbound variable". Keep the two argument lists explicit: the
      # owner p2p path is the common case and must never kill the event reader.
      if [ "$_owner_p2p" -eq 1 ]; then
        _context_snapshot=$(python3 -m core.conversation_context snapshot \
          --conv-key "$conv_key" 2>>"$LOG_FILE" || echo '{}')
      else
        _context_snapshot=$(python3 -m core.conversation_context snapshot \
          --conv-key "$conv_key" --ignore-binding \
          2>>"$LOG_FILE" || echo '{}')
      fi
      logical_context_key=$(echo "$_context_snapshot" | jq -r '.context_key // empty')
      matter_id=$(echo "$_context_snapshot" | jq -r '.matter_id // empty')
      compact_key=$(echo "$_context_snapshot" | jq -r '.compact_key // empty')
      if [ -z "$logical_context_key" ]; then
        logical_context_key="conversation:$conv_key"
        compact_key="$conv_key"
      fi

      session_result=$(get_session_id "$conv_key" "$_session_max" \
        "$logical_context_key" 2>&1)
      session_id=$(echo "$session_result" | tail -1)
      rotated=$(echo "$session_result" | grep ROTATED || true)
      _rotation_reason=$(echo "$rotated" | awk '{print $2}' | tail -1)

      # A bound Lark conversation and its provider session are two pointers to
      # the same Matter. Also record the incoming turn after dedup succeeds.
      ( JV_CONV_KEY="$conv_key" JV_SESSION_ID="$session_id" JV_CONTENT="$content" \
        JV_MSG_ID="$message_id" JV_CONTEXT_KEY="$logical_context_key" \
        JV_MATTER_ID="$matter_id" JARVIS_DIR="$JARVIS_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['JARVIS_DIR'])
from core.matter_bridge import record_turn
from core.matters import link_entity
record_turn(os.environ['JV_CONV_KEY'], 'user', os.environ['JV_CONTENT'],
            os.environ.get('JV_MSG_ID', ''),
            context_key=os.environ['JV_CONTEXT_KEY'],
            matter_id=os.environ.get('JV_MATTER_ID', ''))
if os.environ.get('JV_MATTER_ID'):
    link_entity(os.environ['JV_MATTER_ID'], 'session', os.environ['JV_SESSION_ID'],
                provider='claude', title='Jarvis 飞书会话',
                metadata={'conv_key': os.environ['JV_CONV_KEY']}, actor='lark')
" >>"$LOG_FILE" 2>&1 & )

      if [ -n "$rotated" ]; then
        log_info "Session rotated for $conv_key → $session_id (reason=${_rotation_reason:-unknown})"
      fi

      # Only a capacity rotation within the SAME logical context may summarize
      # the previous physical transcript into the selected compact. A context
      # transition must never write the old topic into the new Matter.
      if [ "$_rotation_reason" = "size" ]; then
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
        if JV_DIR="$JARVIS_DIR" JV_SDIR="$CLAUDE_PROJECT_DIR" JV_KEY="$conv_key" \
          JV_COMPACT_KEY="$compact_key" \
          JV_WORK="$WORK_DIR" python3 -c "
import sys, os, json
sys.path.insert(0, os.environ['JV_DIR'])
from core.compact import generate_compact, get_old_session_id
tracker = json.load(open(os.path.join(os.environ['JV_DIR'], 'active_sessions.json')))
counter = tracker.get(os.environ['JV_KEY'], {}).get('counter', 0)
old_sid = get_old_session_id(os.environ['JV_KEY'], counter)
if old_sid:
    compact = generate_compact(os.environ['JV_DIR'], os.environ['JV_SDIR'],
                               old_sid, os.environ['JV_COMPACT_KEY'], os.environ['JV_WORK'])
    if not compact:
        raise SystemExit(1)
" 2>>"$LOG_FILE" >/dev/null; then
          log_info "Session compact completed for $conv_key"
        else
          log_warn "Session compact failed for $conv_key"
        fi
      fi

      # Sanitize content for log (replace newlines/control chars to prevent log injection)
      _log_content=$(printf '%s' "$content" | tr '\n\r' '  ' | cut -c1-120)
      log_info "[$session_id] Received: $_log_content"

      # ── Engagement tracking (background — independent of the reply, must not
      # block the ack or dispatch; a fresh Python import here costs ~0.5-2s).
      # Groups excluded: a non-owner member's message must not be recorded as
      # the owner's response to the last proactive send — that would inflate
      # engagement scores and confuse the checkin cadence (red-team catch). ──
      if [ "$_owner_p2p" -eq 1 ]; then
        python3 -m core.engagement "$content" >/dev/null 2>>"$LOG_FILE" &
      fi

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
        _pm_text=$(python3 -m core.conversation_context claim-pending \
          --path "$_pm_file" --conv-key "$conv_key" \
          --context-key "$logical_context_key" 2>>"$LOG_FILE" \
          | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    print(f\"[后台任务 {e.get('job_id','')} 已完成 @ {e.get('ts','')}]\n{e.get('summary','')}\n\")
" 2>>"$LOG_FILE")
        if [ -n "$_pm_text" ]; then
          content="${_pm_text}
${content}"
          log_info "Merged pending bg-job result(s) into conversation"
        fi
      fi

      # Dispatch to background — main loop continues immediately
      _dispatch_suffix=$(printf '%s' "$message_id" | shasum -a 256 | cut -c1-16)
      _conv_dispatch_key=$(printf '%s' "$conv_key" | shasum -a 256 | cut -c1-16)
      _dispatch_marker="$JARVIS_DIR/.dispatch_conv_${_conv_dispatch_key}_${_dispatch_suffix}"
      set -m
      handle_message "$conv_key" "$content" "$message_id" "$session_id" \
        "$reaction_id" "$chat_type" "$sender_id" "$logical_context_key" \
        "$matter_id" "$_dispatch_marker" &
      _handler_pid=$!
      set +m
      _handler_token=$(process_start_token "$_handler_pid" 2>/dev/null || true)
      if dispatch_marker_publish "$_dispatch_marker" \
        "$_handler_pid" "$_handler_token"; then
        log_info "[$session_id] Dispatched to background handler (PID $_handler_pid)"
      else
        log_err "[$session_id] Failed to publish dispatch marker; stopping handler"
        terminate_owned_process_group "$_handler_pid" "$_handler_token" "$$" || true
      fi
      done
}

while true; do
  _listener_rc=0
  run_lark_listener_once || _listener_rc=$?
  log_warn "Lark listener exited (rc=$_listener_rc) — reconnecting in 5s"
  sleep 5
done
