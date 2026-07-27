#!/usr/bin/env bash
# Start, stop, or check the Jarvis Dashboard.
#
# Usage:
#   ./dashboard/start.sh          # start in foreground
#   ./dashboard/start.sh --bg     # start in background (daemonized)
#   ./dashboard/start.sh --stop   # stop background process
#   ./dashboard/start.sh --status # check if running
#   ./dashboard/start.sh --install-launchd  # install macOS auto-start

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JARVIS_DIR="$(dirname "$SCRIPT_DIR")"
export JARVIS_DIR
# shellcheck source=../scripts/runtime_env.sh
source "$JARVIS_DIR/scripts/runtime_env.sh"
PIDFILE="$JARVIS_DIR/.dashboard.pid"
PORT=3457

cd "$JARVIS_DIR"

case "${1:-}" in
  --stop)
    if [ -f "$PIDFILE" ]; then
      pid=$(cat "$PIDFILE")
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
        echo "Dashboard stopped (PID $pid)"
      else
        echo "Dashboard not running (stale PID)"
      fi
      rm -f "$PIDFILE"
    else
      echo "No PID file found"
    fi
    ;;

  --status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Dashboard running (PID $(cat "$PIDFILE")) on port $PORT"
      echo "  http://127.0.0.1:$PORT"
    else
      echo "Dashboard not running"
    fi
    ;;

  --bg)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Already running (PID $(cat "$PIDFILE"))"
      exit 0
    fi
    nohup python3 -m dashboard.main > "$JARVIS_DIR/dashboard.log" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Dashboard started (PID $!) on http://127.0.0.1:$PORT"
    ;;

  --install-launchd)
    exec "$JARVIS_DIR/scripts/launchd/install.sh" \
      com.pascal.jarvis.dashboard
    ;;

  --migrate)
    python3 -c "
import sys; sys.path.insert(0, '.')
from dashboard.bookmark_pipeline import migrate_from_jsonl
from pathlib import Path
wl = Path('$JARVIS_DIR').parent / 'memory' / 'system' / 'watchlater.jsonl'
if not wl.exists():
    import sys; sys.path.insert(0, '.')
    from core.claude_projects import heartbeat_memory_dir
    wl = heartbeat_memory_dir() / 'system' / 'watchlater.jsonl'
if wl.exists():
    count = migrate_from_jsonl(str(wl))
    print(f'Migrated {count} bookmarks from {wl}')
else:
    print('No watchlater.jsonl found')
"
    ;;

  ""|--fg)
    echo "Starting Jarvis Dashboard on http://127.0.0.1:$PORT"
    python3 -m dashboard.main
    ;;

  *)
    echo "Usage: $0 [--bg|--stop|--status|--install-launchd|--migrate]"
    exit 1
    ;;
esac
