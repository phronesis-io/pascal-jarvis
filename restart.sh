#!/usr/bin/env bash
# restart.sh — Graceful restart of Jarvis runtime services.
#
# Usage:
#   ./restart.sh           # Governed deploy of daemon, bot, and installed UI
#   ./restart.sh --full    # Alias for the governed full-runtime deploy
#   ./restart.sh --runtime # Restart bot tree at the already-deployed revision
#   ./restart.sh --status # Just show current process status
#
set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
export JARVIS_DIR
# shellcheck source=scripts/runtime_env.sh
source "$JARVIS_DIR/scripts/runtime_env.sh"
BOT_PID_FILE="$JARVIS_DIR/.bot.pid"
DAEMON_PID_FILE="$JARVIS_DIR/.daemon.pid"
LOG="/tmp/jarvis_restart.log"
FULL_RUNTIME_COMPONENTS=(daemon bot heartbeat-loop)
RESTART_CONFIRMED=0

# ── Helpers ──────────────────────────────────────────────────────────

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[0;90m%s\033[0m\n' "$*"; }

status() {
  echo "=== Jarvis Process Status ==="
  echo ""

  # Daemon
  if [ -f "$DAEMON_PID_FILE" ]; then
    daemon_pid=$(cat "$DAEMON_PID_FILE" 2>/dev/null)
    if kill -0 "$daemon_pid" 2>/dev/null; then
      green "  daemon.py: running (PID $daemon_pid)"
    else
      red "  daemon.py: stale PID file (PID $daemon_pid not alive)"
    fi
  else
    dim "  daemon.py: no PID file"
    # Check via pgrep
    daemon_pid=$(pgrep -f "$JARVIS_DIR/daemon\\.py" 2>/dev/null | head -1 || true)
    if [ -n "$daemon_pid" ]; then
      green "  daemon.py: running (PID $daemon_pid, no PID file)"
    else
      red "  daemon.py: not running"
    fi
  fi

  # Bot
  # PID file format is "PID BOOT_TIMESTAMP" — read only the first field.
  if [ -f "$BOT_PID_FILE" ]; then
    bot_pid=$(awk '{print $1}' "$BOT_PID_FILE" 2>/dev/null)
    if kill -0 "$bot_pid" 2>/dev/null; then
      green "  bot.sh:    running (PID $bot_pid)"
    else
      red "  bot.sh:    stale PID file (PID $bot_pid not alive)"
    fi
  else
    bot_pid=$(pgrep -f "bash.*$JARVIS_DIR/bot\\.sh" 2>/dev/null | head -1 || true)
    if [ -n "$bot_pid" ]; then
      green "  bot.sh:    running (PID $bot_pid, no PID file)"
    else
      red "  bot.sh:    not running"
    fi
  fi

  # Lark listener
  # Anchor to the current bot PID. Broad pgrep patterns can match diagnostics
  # or orphaned sidecars, which made status report green while bot/admin were
  # actually gone.
  lark_pid=""
  if [ -n "${bot_pid:-}" ] && kill -0 "$bot_pid" 2>/dev/null; then
    lark_pid=$(python3 - "$bot_pid" <<'PY' 2>/dev/null || true
import subprocess, sys
bot = int(sys.argv[1])
r = subprocess.run(["ps", "ax", "-o", "pid=,ppid=,command="],
                   capture_output=True, text=True, timeout=5)
procs = {}
for line in r.stdout.splitlines():
    parts = line.strip().split(None, 2)
    if len(parts) < 3:
        continue
    try:
        procs[int(parts[0])] = (int(parts[1]), parts[2])
    except ValueError:
        pass
def owned(pid):
    seen = set()
    while pid and pid not in seen:
        if pid == bot:
            return True
        seen.add(pid)
        pid = procs.get(pid, (0, ""))[0]
    return False
for pid, (_, cmd) in procs.items():
    if ("lark_event_sidecar.py" in cmd or "lark-cli event" in cmd) and owned(pid):
        print(pid)
        break
PY
)
  fi
  if [ -n "$lark_pid" ]; then
    green "  lark-cli:  running (PID $lark_pid)"
  else
    dim "  lark-cli:  not running"
  fi

  echo ""
}

confirm_restart() {
  if [ "$RESTART_CONFIRMED" -eq 1 ]; then
    return 0
  fi

  # Confirm before any launchd definition refresh or process mutation.
  active_locks=$(find "$JARVIS_DIR" -maxdepth 1 -name '.session_lock_*' 2>/dev/null | wc -l | tr -d ' ')
  if [ "$active_locks" -gt 0 ] && [ "$ASSUME_YES" -ne 1 ]; then
    echo ""
    red "  ⚠️  $active_locks active conversation(s) in progress!"
    red "  Restarting will DESTROY the in-flight Claude response."
    if [ ! -t 0 ]; then
      # Non-interactive caller (cron, admin trigger, Claude): `read` would hit
      # EOF and set -e would abort with no message. Refuse loudly instead.
      red "  Non-interactive session — refusing to kill an in-flight conversation."
      red "  Re-run with --yes to force."
      exit 1
    fi
    echo -n "  Continue? [y/N] "
    read -r _confirm
    if [ "$_confirm" != "y" ] && [ "$_confirm" != "Y" ]; then
      echo "  Aborted."
      exit 0
    fi
  fi
  RESTART_CONFIRMED=1
}

kill_bot() {
  confirm_restart

  echo "Stopping bot.sh and children..."

  # Kill by PID file first (format is "PID BOOT_TIMESTAMP" — read first field only)
  if [ -f "$BOT_PID_FILE" ]; then
    bot_pid=$(awk '{print $1}' "$BOT_PID_FILE" 2>/dev/null)
    if [ -n "$bot_pid" ] && kill -0 "$bot_pid" 2>/dev/null; then
      kill -TERM "$bot_pid" 2>/dev/null || true
    fi
  fi

  # Kill all bot.sh and lark-cli event processes
  pkill -f "bash.*$JARVIS_DIR/bot\\.sh" 2>/dev/null || true
  pkill -f "lark-cli event|lark_event_sidecar" 2>/dev/null || true
  # Reap orphaned python children (red-team fix): the REQ-42 restart hand-off
  # could leave a watchdog-relaunched heartbeat_loop / ef_stream_loop / admin
  # the parent's cleanup trap no longer knows about. The heartbeat_loop
  # singleton flock makes a duplicate exit anyway; reaping here guarantees no
  # stale loop survives a restart.
  pkill -f "core\\.heartbeat_loop" 2>/dev/null || true
  pkill -f "core\\.ef_stream_loop" 2>/dev/null || true
  pkill -f "eigenflux stream" 2>/dev/null || true
  pkill -f "$JARVIS_DIR/admin\\.py" 2>/dev/null || true

  # Kill stuck claude sessions (lock format: "<pid> <token>"). Verify process
  # identity before kill — locks survive crashed handlers, and killing a
  # recycled PID blind hits an arbitrary user process (7/7 audit; same class
  # as af35420). The lock holds a backgrounded bot.sh pipeline subshell, so
  # ps shows 'bash .../bot.sh' (the parent argv), never 'claude' — match ONLY
  # the repo's bot.sh path. (A '*claude*' fallback arm was removed 7/8
  # red-team: a real lock holder can never show 'claude', so it could only
  # ever match a recycled PID landing on an unrelated claude process — e.g.
  # an interactive Claude Code session — the banned substring-match class.)
  for lock in "$JARVIS_DIR"/.session_lock_*; do
    [ -f "$lock" ] || continue
    pid=$(awk '{print $1}' "$lock" 2>/dev/null)
    if [ -n "$pid" ]; then
      pid_args=$(ps -p "$pid" -o args= 2>/dev/null || true)
      case "$pid_args" in
        *bash*"$JARVIS_DIR/bot.sh"*)
          kill "$pid" 2>/dev/null || true
          ;;
      esac
    fi
    rm -f "$lock"
  done

  # Clean PID file
  rm -f "$BOT_PID_FILE"

  # Wait for processes to die
  sleep 2

  # A helper may still be inside its graceful child wait when the two-second
  # hand-off expires. Leaving it alive is worse than a hard stop: the old and
  # new EigenFlux streams then replace each other forever, while orphaned
  # heartbeat/admin processes keep executing the previous release. The bot is
  # still stopped here and the deploy guard is active, so this exact-identity
  # sweep cannot hit a newly started runtime.
  force_kill_pattern() {
    local label="$1" pattern="$2"
    if pgrep -f "$pattern" >/dev/null 2>&1; then
      echo "  Force killing lingering $label..."
      pkill -9 -f "$pattern" 2>/dev/null || true
    fi
  }
  force_kill_pattern "heartbeat loop" "core\\.heartbeat_loop"
  force_kill_pattern "EigenFlux loop" "core\\.ef_stream_loop"
  force_kill_pattern "EigenFlux stream child" "eigenflux stream"
  force_kill_pattern "Lark sidecar" "lark-cli event|lark_event_sidecar"
  force_kill_pattern "admin process" "$JARVIS_DIR/admin\\.py"

  # Verify
  if pgrep -f "bash.*$JARVIS_DIR/bot\\.sh" >/dev/null 2>&1; then
    echo "  Force killing remaining bot processes..."
    pkill -9 -f "bash.*$JARVIS_DIR/bot\\.sh" 2>/dev/null || true
    sleep 1
  fi

  green "  Bot stopped."
}

kill_daemon() {
  echo "Stopping daemon..."
  if [ -f "$DAEMON_PID_FILE" ]; then
    daemon_pid=$(cat "$DAEMON_PID_FILE" 2>/dev/null)
    if [ -n "$daemon_pid" ] && kill -0 "$daemon_pid" 2>/dev/null; then
      kill -TERM "$daemon_pid" 2>/dev/null || true
    fi
    rm -f "$DAEMON_PID_FILE"
  fi
  pkill -f "$JARVIS_DIR/daemon\\.py" 2>/dev/null || true
  sleep 2
  green "  Daemon stopped."
}

start_bot() {
  # Clear Python bytecode cache — prevents AttributeError after code updates
  find "$JARVIS_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

  # Rotate restart log (grows unbounded otherwise — was 1.2MB)
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 500000 ]; then
    tail -200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi

  echo "Starting bot.sh..."
  # cd into JARVIS_DIR first: bot.sh's bg helpers run as `python3 -m core.X`,
  # which need CWD=JARVIS_DIR to import `core/`. Without this, a restart kicked
  # off from another directory (e.g. Claude running restart.sh from WORK_DIR)
  # brings the bot up broken and triggers a restart loop. (bot.sh now also
  # anchors its own CWD, but keep this as defense in depth.)
  cd "$JARVIS_DIR" || { red "  FATAL: cannot cd to JARVIS_DIR"; return 1; }
  python3 - "$JARVIS_DIR" "$LOG" <<'PY'
import subprocess
import sys

jarvis_dir, log_path = sys.argv[1], sys.argv[2]
log_fd = open(log_path, "a")
subprocess.Popen(
    ["bash", f"{jarvis_dir}/bot.sh"],
    stdout=log_fd,
    stderr=subprocess.STDOUT,
    cwd=jarvis_dir,
    start_new_session=True,
)
log_fd.close()
PY
  sleep 3

  if pgrep -f "bash.*$JARVIS_DIR/bot\\.sh" >/dev/null 2>&1; then
    green "  Bot started. Log: $LOG"
  else
    red "  Bot failed to start! Check: tail -20 $LOG"
    return 1
  fi
}

settle_bot() {
  local seconds="${1:-75}"
  local report="/tmp/jarvis_restart_components.$$"
  echo "Settling bot for ${seconds}s..."
  sleep "$seconds"
  if python3 -m core.components --critical > "$report" 2>&1; then
    green "  Bot settled; critical components healthy."
    rm -f "$report"
    if python3 -m core.deploy smoke --timeout 3 >/dev/null; then
      green "  Unified delivery smoke passed."
    else
      red "  Unified delivery smoke failed."
      return 1
    fi
    if python3 -m core.deploy verify \
        --require bot --require heartbeat-loop >/dev/null; then
      green "  Bot runtime versions match the deployed commit."
    else
      red "  Runtime version verification failed."
      python3 -m core.deploy verify \
        --require bot --require heartbeat-loop || true
      return 1
    fi
  else
    red "  Bot failed post-start settle check:"
    cat "$report"
    rm -f "$report"
    return 1
  fi
}

start_daemon() {
  echo "Starting daemon.py..."
  cd "$JARVIS_DIR"
  nohup python3 "$JARVIS_DIR/daemon.py" >> "$JARVIS_DIR/daemon.log" 2>&1 &
  sleep 2

  if pgrep -f "$JARVIS_DIR/daemon\\.py" >/dev/null 2>&1; then
    green "  Daemon started."
  else
    red "  Daemon failed to start! Check: tail -20 daemon.log"
    return 1
  fi
}

restart_daemon() {
  # In production the daemon is launchd-owned (KeepAlive). pkill + nohup here
  # races the KeepAlive respawn against an unsupervised twin — the 6/12
  # double-daemon class — so when the job is loaded, hand the restart to
  # launchd instead. Don't touch .daemon.pid on this path: racing an rm
  # against the fresh daemon's pidfile write can delete the winner's file
  # (acquire_singleton handles stale leftovers itself). kickstart can fail
  # from a non-GUI context (SSH without a user session) — fall through to
  # the manual kill+nohup path then, same as when the job isn't loaded.
  if launchctl print "gui/$UID/com.pascal.jarvis.daemon" >/dev/null 2>&1; then
    echo "Restarting daemon via launchd..."
    if launchctl kickstart -k "gui/$UID/com.pascal.jarvis.daemon" 2>/dev/null; then
      sleep 2
      if pgrep -f "$JARVIS_DIR/daemon\\.py" >/dev/null 2>&1; then
        green "  Daemon restarted (launchd)."
        return 0
      fi
      red "  Daemon not up after kickstart! Check: tail -20 /tmp/jarvis-daemon-stderr.log"
      return 1
    fi
    red "  launchctl kickstart failed — falling back to manual restart."
  fi
  kill_daemon
  start_daemon
}

# Surfaces retired from this deploy path — labels here are torn down, never
# refreshed: dashboard :3457 (2026-08-21), mobile gateway :3458 and its
# Jarvis-owned userspace tailscaled service (2026-08-11, REQ-120). An
# installed KeepAlive job whose package is deleted
# crash-loops on ModuleNotFoundError every ~10s with no supervision surface
# left to see it, so a governed deploy removes any leftover job + definition
# in code, not in deploy prose. Idempotent: a clean machine logs nothing.
RETIRED_LABELS=(
  "com.pascal.jarvis.dashboard"
  "com.pascal.jarvis.mobile-gateway"
  "com.pascal.jarvis.tailscaled"
)

remove_retired_launchd_jobs() {
  local label job plist
  if ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi
  for label in "${RETIRED_LABELS[@]}"; do
    job="gui/$UID/$label"
    plist="$HOME/Library/LaunchAgents/$label.plist"
    if launchctl print "$job" >/dev/null 2>&1; then
      launchctl bootout "$job" 2>/dev/null || true
      echo "Removed retired launchd job: $label"
    fi
    if [ -f "$plist" ]; then
      rm -f "$plist"
      echo "Removed retired launchd definition: $plist"
    fi
  done
}

refresh_launchd_definitions() {
  local installer="$JARVIS_DIR/scripts/launchd/install.sh"
  local labels=()
  local label
  local job
  local plist
  local probe_rc

  if ! command -v launchctl >/dev/null 2>&1; then
    dim "  launchd unavailable; definition refresh skipped."
    return 0
  fi

  remove_retired_launchd_jobs

  # Only refresh services already enabled on this installation. This keeps
  # --full from silently enabling optional UI surfaces on a fresh clone while
  # ensuring tracked ProgramArguments/environment changes reach launchd before
  # their processes are restarted. Retired labels live in RETIRED_LABELS
  # above and are removed, never refreshed.
  for label in \
      "com.pascal.jarvis.daemon"; do
    job="gui/$UID/$label"
    plist="$HOME/Library/LaunchAgents/$label.plist"
    if launchd_job_state "$job"; then
      if [ ! -f "$plist" ]; then
        red "  $label is loaded without an installed plist; refusing an update that cannot roll back."
        return 1
      fi
      labels+=("$label")
    else
      probe_rc=$?
      if [ "$probe_rc" -eq 1 ] && [ -f "$plist" ]; then
        labels+=("$label")
      elif [ "$probe_rc" -eq 2 ]; then
        red "  Cannot inspect $label: $LAUNCHD_PROBE_DETAIL"
        return 1
      fi
    fi
  done

  if [ "${#labels[@]}" -eq 0 ]; then
    dim "  launchd services: none installed; definition refresh skipped."
    return 0
  fi

  echo "Refreshing installed launchd definitions..."
  if ! "$installer" "${labels[@]}"; then
    red "  Failed to refresh launchd definitions; bot was not stopped."
    return 1
  fi
  green "  Installed launchd definitions are current."
}

LAUNCHD_PROBE_DETAIL=""
launchd_job_state() {
  local job="$1"
  local detail
  if detail=$(launchctl print "$job" 2>&1); then
    LAUNCHD_PROBE_DETAIL=""
    return 0
  fi
  case "$detail" in
    *"Could not find service"*|*"could not find service"*|\
    *"Service cannot be found"*|*"service cannot be found"*)
      LAUNCHD_PROBE_DETAIL="$detail"
      return 1
      ;;
    *)
      LAUNCHD_PROBE_DETAIL="${detail:-launchctl print failed without details}"
      return 2
      ;;
  esac
}

record_release_receipt() {
  local mode="$1"
  local report="/tmp/jarvis_release_receipt.json"
  echo "Persisting joined release evidence..."
  if python3 -m core.deploy receipt \
      --gate-evidence /tmp/jarvis_release_gate.json \
      --mode "$mode" >"$report"; then
    green "  Release receipt persisted."
    return 0
  fi
  red "  Release receipt verification failed."
  cat "$report"
  return 1
}

verify_full_runtime() {
  local verify_args=()
  local component
  if python3 -c \
      'from core.config import Config; raise SystemExit(0 if Config().get("admin.enabled") else 1)' \
      >/dev/null 2>&1; then
    FULL_RUNTIME_COMPONENTS+=(admin)
  fi
  for component in "${FULL_RUNTIME_COMPONENTS[@]}"; do
    verify_args+=(--require "$component")
  done

  echo "Verifying all resident runtime versions..."
  if python3 -m core.deploy verify "${verify_args[@]}" >/dev/null; then
    green "  All runtime versions match the deployed commit."
    return 0
  fi

  red "  Full runtime version verification failed."
  python3 -m core.deploy verify "${verify_args[@]}" || true
  return 1
}

prepare_codex_frontstage() {
  if ! command -v codex >/dev/null 2>&1; then
    dim "  Codex CLI unavailable; frontstage plugin install skipped."
    return 0
  fi

  echo "Preparing Codex frontstage integration..."
  if ! "$JARVIS_PYTHON" - <<'PY' >/dev/null 2>&1
from importlib.metadata import PackageNotFoundError, version

try:
    major = int(version("mcp").split(".", 1)[0])
except (PackageNotFoundError, TypeError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if major == 2 else 1)
PY
  then
    echo "  Installing the supported MCP runtime..."
    "$JARVIS_PYTHON" -m pip install --disable-pip-version-check \
      "mcp>=2,<3"
  fi

  "$JARVIS_DIR/scripts/install_codex_integration.sh" >/dev/null
  if codex plugin list 2>/dev/null \
       | grep -q 'jarvis-matters@pascal-jarvis.*installed, enabled'; then
    if "$JARVIS_PYTHON" "$JARVIS_DIR/scripts/check_codex_frontstage.py" \
         >/dev/null; then
      green "  Codex Jarvis Matters plugin and MCP handshake are healthy."
      return 0
    fi
    red "  Codex MCP handshake failed; runtime was not stopped."
    return 1
  fi
  red "  Codex plugin verification failed; runtime was not stopped."
  return 1
}

# ── Main ─────────────────────────────────────────────────────────────

# --yes anywhere in argv skips the in-flight-conversation confirmation
# (needed by non-interactive callers: admin restart trigger, automation).
ASSUME_YES=0
_args=()
for _a in "$@"; do
  if [ "$_a" = "--yes" ] || [ "$_a" = "-y" ]; then
    ASSUME_YES=1
  else
    _args+=("$_a")
  fi
done
set -- "${_args[@]:-}"

# Deploy guard (REQ-42): while this script works, the daemon must not "fix"
# the intentionally-half-down stack (6/12: daemon killed a healthy bot twice
# mid-deploy). Restart paths touch the flag; status/help do not. The trap
# clears it even when a kill/start step fails (set -e).
_set_deploy_guard() {
  touch "$JARVIS_DIR/.deploying"
  trap 'rm -f "$JARVIS_DIR/.deploying"' EXIT
}

_verify_release_gate() {
  echo "Verifying PR, CI, review, and branch-protection evidence..."
  if python3 -m core.release_gate >/tmp/jarvis_release_gate.json; then
    green "  Release gate passed."
  else
    red "  Release gate failed; runtime was not touched."
    cat /tmp/jarvis_release_gate.json
    exit 1
  fi
}

_verify_runtime_only_gate() {
  local dirty
  dirty=$(git -C "$JARVIS_DIR" status --porcelain --untracked-files=all)
  if [ -n "$dirty" ]; then
    red "  Runtime-only restart refused: source worktree is not clean."
    echo "$dirty"
    exit 1
  fi
  echo "Verifying the running bot already matches this revision..."
  if python3 -m core.deploy verify \
      --allow-config-changes \
      --require bot --require heartbeat-loop >/tmp/jarvis_runtime_restart_gate.json; then
    green "  Same-revision runtime gate passed."
  else
    red "  Runtime-only restart refused: code deployment is required."
    cat /tmp/jarvis_runtime_restart_gate.json
    echo "  Use the governed ./restart.sh path after merge, CI, and review."
    exit 1
  fi
}

governed_deploy() {
  local heading="${1:-Governed Full-Runtime Deploy}"
  echo "=== $heading ==="
  echo ""
  _verify_release_gate
  _set_deploy_guard
  # Confirmation must precede definition refresh because installing a changed
  # plugin mutates Codex config and a changed plist restarts its launchd job.
  confirm_restart
  prepare_codex_frontstage
  refresh_launchd_definitions
  kill_bot
  echo ""
  # Fault-tolerant on purpose (2026-07-09 red-team [10]): under set -e a
  # restart_daemon failure here would abort AFTER kill_bot but BEFORE
  # start_bot — bot left dead with the daemon (its reviver/alert channel)
  # also down. The bot must always be started; a broken daemon is loud but
  # survivable.
  restart_daemon || red "  DAEMON RESTART FAILED — starting bot anyway; check: tail -20 /tmp/jarvis-daemon-stderr.log"
  start_bot
  settle_bot
  # Retired components never re-register, so a dead runtime_versions row
  # would turn every unfiltered `core.deploy verify` (the --runtime gate)
  # red forever. Deregistering an already-absent row is a no-op.
  if python3 -m core.deploy deregister dashboard mobile-gateway \
      >/dev/null 2>&1; then
    dim "  Retired runtime registrations cleared (dashboard, mobile-gateway)."
  fi
  verify_full_runtime
  record_release_receipt governed
  echo ""
  status
}

case "${1:-}" in
  --status|-s)
    status
    ;;
  --full|-f)
    governed_deploy "Governed Full-Runtime Deploy"
    ;;
  --runtime|-r)
    echo "=== Same-Revision Runtime Restart ==="
    echo ""
    _verify_release_gate
    _verify_runtime_only_gate
    _set_deploy_guard
    kill_bot
    echo ""
    start_bot
    settle_bot
    verify_full_runtime
    record_release_receipt runtime
    echo ""
    status
    ;;
  --help|-h)
    echo "Usage: ./restart.sh [--full|--runtime|--status|--help] [--yes]"
    echo ""
    echo "  (no args)   Governed deploy of daemon, bot, and installed UI"
    echo "  --full      Alias for the governed full-runtime deploy"
    echo "  --runtime   Restart bot tree only when live code already matches HEAD"
    echo "  --status    Show current process status"
    echo "  --yes, -y   Skip the in-flight-conversation confirmation"
    ;;
  "")
    governed_deploy "Governed Full-Runtime Deploy"
    ;;
  *)
    red "Unknown option: $1"
    echo "Usage: ./restart.sh [--full|--runtime|--status|--help] [--yes]"
    exit 2
    ;;
esac
