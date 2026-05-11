#!/usr/bin/env bash
# Jarvis Watchdog — independent supervisor process.
# Runs via launchd, completely separate from bot.sh.
#
# Responsibilities:
# 1. Check if bot.sh is running → restart if dead
# 2. Check heartbeat freshness → restart if stuck
# 3. Check Lark listener → restart if disconnected
# 4. Log health status
#
# Designed to be called every 5 minutes by launchd.

set -uo pipefail

JARVIS_DIR="${JARVIS_DIR:-/Users/pascal/Desktop/jarvis/repos/pascal-jarvis}"
LOG_FILE="$JARVIS_DIR/watchdog.log"
HEARTBEAT_LOG="/tmp/jarvis_restart.log"
MAX_HEARTBEAT_AGE=600  # seconds — if no heartbeat beat in 10 min, consider stuck
MAX_LOG_LINES=500

# Rotate log if too large
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$MAX_LOG_LINES" ]; then
  tail -200 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# ── Check 1: Is bot.sh running? ──
bot_pids=$(pgrep -f 'bash.*bot\.sh' 2>/dev/null || true)
if [ -z "$bot_pids" ]; then
  log "ALERT: bot.sh not running — restarting"
  # Kill any orphaned lark-cli or admin processes
  pkill -f 'lark-cli event' 2>/dev/null || true
  pkill -f 'admin\.py' 2>/dev/null || true
  sleep 2
  cd "$JARVIS_DIR"
  nohup bash bot.sh >> "$HEARTBEAT_LOG" 2>&1 &
  log "RESTART: bot.sh started (PID $!)"
  exit 0
fi

# ── Check 2: Is heartbeat producing output? ──
if [ -f "$HEARTBEAT_LOG" ]; then
  last_beat=$(grep '\[heartbeat\].*Beat sent\|heartbeat.*Starting' "$HEARTBEAT_LOG" 2>/dev/null | tail -1)
  if [ -n "$last_beat" ]; then
    # Extract timestamp
    beat_ts=$(echo "$last_beat" | grep -o '\[20[0-9-]* [0-9:]*\]' | tr -d '[]')
    if [ -n "$beat_ts" ]; then
      beat_epoch=$(date -j -f '%Y-%m-%d %H:%M:%S' "$beat_ts" '+%s' 2>/dev/null || echo 0)
      now_epoch=$(date '+%s')
      age=$((now_epoch - beat_epoch))
      if [ "$age" -gt "$MAX_HEARTBEAT_AGE" ]; then
        log "ALERT: heartbeat stale (${age}s old, max ${MAX_HEARTBEAT_AGE}s) — restarting"
        pkill -f 'bash.*bot\.sh' 2>/dev/null || true
        pkill -f 'lark-cli event' 2>/dev/null || true
        sleep 3
        cd "$JARVIS_DIR"
        nohup bash bot.sh >> "$HEARTBEAT_LOG" 2>&1 &
        log "RESTART: bot.sh restarted (PID $!)"
        exit 0
      fi
    fi
  fi
fi

# ── Check 3: Is Lark event listener connected? ──
lark_pids=$(pgrep -f 'lark-cli event' 2>/dev/null || true)
if [ -z "$lark_pids" ]; then
  log "WARN: Lark event listener not running (bot.sh may be in heartbeat-only mode)"
fi

# ── Check 4: Recent errors? ──
if [ -f "$HEARTBEAT_LOG" ]; then
  recent_errors=$(tail -50 "$HEARTBEAT_LOG" 2>/dev/null | grep -i 'FATAL\|panic\|Traceback' | tail -3)
  if [ -n "$recent_errors" ]; then
    log "WARN: Recent errors detected: $(echo "$recent_errors" | head -1)"
  fi
fi

# All good
log "OK: bot($(echo "$bot_pids" | wc -w | tr -d ' ') procs) heartbeat(alive) lark($(echo "$lark_pids" | wc -w | tr -d ' ') procs)"
