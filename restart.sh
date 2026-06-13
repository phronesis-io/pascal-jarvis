#!/usr/bin/env bash
# restart.sh — Graceful restart of Jarvis bot (and optionally daemon).
#
# Usage:
#   ./restart.sh          # Restart bot only (daemon stays alive, restarts bot)
#   ./restart.sh --full   # Restart both daemon and bot
#   ./restart.sh --status # Just show current process status
#
set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_PID_FILE="$JARVIS_DIR/.bot.pid"
DAEMON_PID_FILE="$JARVIS_DIR/.daemon.pid"
LOG="/tmp/jarvis_restart.log"

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
  lark_pid=$(pgrep -f "lark-cli event|lark_event_sidecar" 2>/dev/null | head -1 || true)
  if [ -n "$lark_pid" ]; then
    green "  lark-cli:  running (PID $lark_pid)"
  else
    dim "  lark-cli:  not running"
  fi

  echo ""
}

kill_bot() {
  # Check if user has an active conversation — warn before killing
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

  # Kill stuck claude sessions (lock format: "<pid> <token>")
  for lock in "$JARVIS_DIR"/.session_lock_*; do
    [ -f "$lock" ] || continue
    pid=$(awk '{print $1}' "$lock" 2>/dev/null)
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$lock"
  done

  # Clean PID file
  rm -f "$BOT_PID_FILE"

  # Wait for processes to die
  sleep 2

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
  nohup bash "$JARVIS_DIR/bot.sh" >> "$LOG" 2>&1 &
  sleep 3

  if pgrep -f "bash.*$JARVIS_DIR/bot\\.sh" >/dev/null 2>&1; then
    green "  Bot started. Log: $LOG"
  else
    red "  Bot failed to start! Check: tail -20 $LOG"
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

case "${1:-}" in
  --status|-s)
    status
    ;;
  --full|-f)
    echo "=== Full Restart (daemon + bot) ==="
    echo ""
    _set_deploy_guard
    kill_bot
    kill_daemon
    echo ""
    start_daemon
    start_bot
    echo ""
    status
    ;;
  --help|-h)
    echo "Usage: ./restart.sh [--full|--status|--help] [--yes]"
    echo ""
    echo "  (no args)   Restart bot only (daemon auto-detects and stays)"
    echo "  --full      Restart both daemon and bot"
    echo "  --status    Show current process status"
    echo "  --yes, -y   Skip the in-flight-conversation confirmation"
    ;;
  *)
    echo "=== Bot Restart ==="
    echo ""
    _set_deploy_guard
    kill_bot
    echo ""
    start_bot
    echo ""
    status
    ;;
esac
