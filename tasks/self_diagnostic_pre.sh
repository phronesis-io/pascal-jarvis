#!/usr/bin/env bash
# Pre-hook: collect system health data for self-diagnostic
WORK_DIR="${WORK_DIR:-$(cd "$JARVIS_DIR/.." 2>/dev/null && pwd || echo "$JARVIS_DIR")}"
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.claude/projects/-Users-pascal-Desktop-jarvis/memory}"

# REQ-39: leave a copy of this report for the deterministic alert post-script
# (tasks/self_diagnostic_post.py scans it for ⚠️ lines — detection and
# delivery are split so SILENT_TASKS can never mute a real alarm again).
exec > >(tee "$JARVIS_DIR/.diag_last_pre.txt")

echo "=== SYSTEM HEALTH CHECK ==="
echo "Time: $(date '+%Y-%m-%d %H:%M %A')"
echo ""

# 1. EigenFlux profile staleness
echo "--- EigenFlux Profile ---"
profile_ts=$(eigenflux profile show 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
ts = d.get('profile', {}).get('updated_at', 0) / 1000
from datetime import datetime
print(datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M'))
" 2>/dev/null || echo "unknown")
echo "Last updated: $profile_ts"

# 2. Calendar freshness
echo ""
echo "--- Calendar ---"
cal_file="$MEMORY_DIR/calendar_today.md"
[ ! -f "$cal_file" ] && cal_file="$MEMORY_DIR/hot/calendar_today.md"
if [ -f "$cal_file" ]; then
  cal_sync=$(grep -o 'synced [0-9-]* [0-9:]*' "$cal_file" | head -1)
  echo "Last sync: $cal_sync"
else
  echo "⚠️ No calendar_today.md found"
fi

# 3. Repos — last pull times
echo ""
echo "--- Repos ---"
for repo in "$WORK_DIR/repos"/*/; do
  [ -d "$repo/.git" ] || continue
  name=$(basename "$repo")
  last_fetch=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$repo/.git/FETCH_HEAD" 2>/dev/null || echo "never")
  echo "  $name: last fetch $last_fetch"
done

# 4. Personal site
echo ""
echo "--- Personal Site ---"
if [ -d "$WORK_DIR/repos/huyongyi-cpu.github.io" ]; then
  last_commit=$(git -C "$WORK_DIR/repos/huyongyi-cpu.github.io" log -1 --format="%ci %s" 2>/dev/null || echo "unknown")
  echo "Last commit: $last_commit"
else
  echo "⚠️ Repo not found"
fi

# 5. Memory health
echo ""
echo "--- Memory ---"
hot_count=$(ls "$MEMORY_DIR/hot/"*.md 2>/dev/null | wc -l | tr -d ' ')
warm_count=$(ls "$MEMORY_DIR/warm/"*.md 2>/dev/null | wc -l | tr -d ' ')
echo "Hot files: $hot_count | Warm files: $warm_count"
echo "Behavioral rules: $([ -f "$MEMORY_DIR/hot/behavioral_rules.md" ] && echo "✓" || echo "✗")"

# 6. EigenFlux stream health
echo ""
echo "--- EigenFlux Stream ---"
# Match only processes whose command starts with "eigenflux stream"
# (avoids matching Claude prompts that mention "eigenflux stream" in text)
_stream_pids=$(ps -eo pid,comm,args | awk '$2 == "eigenflux" && $3 ~ /eigenflux/ && $4 == "stream" {print $1}')
_stream_count=$(echo "$_stream_pids" | grep -c '[0-9]' 2>/dev/null || echo 0)
if [ "$_stream_count" -eq 1 ]; then
  _stream_pid=$(echo "$_stream_pids" | head -1)
  _stream_uptime=$(ps -p "$_stream_pid" -o etime= 2>/dev/null | tr -d ' ')
  echo "✓ Stream running (PID $_stream_pid, uptime $_stream_uptime)"
  # Process alive ≠ connected: on 2026-06-11 the stream retried 'Connect
  # failed: EOF' for an extended outage while this check showed green.
  _recent_fails=$(tail -50 "$JARVIS_DIR/jarvis.log" 2>/dev/null | grep -c 'Connect failed' || true)
  _recent_ok=$(tail -50 "$JARVIS_DIR/jarvis.log" 2>/dev/null | grep -c 'Connected\. Streaming' || true)
  if [ "${_recent_fails:-0}" -ge 3 ] && [ "${_recent_ok:-0}" -eq 0 ]; then
    echo "⚠️ Stream process alive but CONNECTION FAILING (${_recent_fails} recent retries, 0 successes) — EigenFlux messages are NOT flowing (likely server-side outage)"
  fi
elif [ "$_stream_count" -eq 0 ]; then
  echo "⚠️ Stream NOT running — real-time messages will not be received"
else
  echo "⚠️ $_stream_count stream processes found — competing connections cause 'Connection replaced' loop"
fi

# 7a. Card callback support watch: lark-cli 1.0.44 can't consume
#     card.action.trigger (larksuite/cli#1051) — callback buttons are disabled
#     across the product. Flag loudly the moment an upgrade adds support so
#     they can be re-enabled (PRD REQ-17).
if lark-cli event list 2>/dev/null | grep -qi 'card'; then
  echo ""
  echo "🎉 lark-cli now lists card events — card callback buttons can be re-enabled!"
  echo "   (re-add the 收藏 button in tasks/content_recommend_post.py, add card.action.trigger consumption; see PRD REQ-17)"
fi

# 7. Channel watermarks (REQ-12) — flags starved tasks / open circuits /
#    delivery failures so dead channels are caught here, not by the user.
echo ""
(cd "$JARVIS_DIR" && JARVIS_DIR="$JARVIS_DIR" python3 -m core.watermarks 2>/dev/null) \
  || echo "--- Channel Watermarks ---
  ⚠️ watermark check itself failed to run"

# 7b. Component manifest (REQ-40): the single source of truth for "what
#     should be running". Failures print as ⚠️ lines → REQ-39 alert path.
echo ""
(cd "$JARVIS_DIR" && python3 -m core.components 2>/dev/null) \
  || true  # non-zero exit = failing components; the ⚠️ lines carry the signal

# 7c. Intent breach daily check (REQ-35): silently-expired commitments in the
#     last 24h mean the retry+breach pipeline itself is broken — page loudly.
echo ""
echo "--- Intent Lifecycle ---"
_dropped=$(sqlite3 "file:$JARVIS_DIR/data/jarvis.db?mode=ro" \
  "SELECT COUNT(*) FROM intentions WHERE status='expired' \
   AND (last_error LIKE 'auto-expired%' OR last_error LIKE '%expired after%attempts%') \
   AND triggered_at >= datetime('now','-1 day')" 2>/dev/null || echo "?")
if [ "$_dropped" != "?" ] && [ "${_dropped:-0}" -gt 0 ]; then
  echo "⚠️ $_dropped intent(s) dropped in the last 24h (expired after retries/stuck) — check breach cards went out"
else
  echo "✓ No silently dropped intents in the last 24h"
fi

# 8. CLI versions
echo ""
echo "--- CLI Versions ---"
_claude_ver=$(claude --version 2>/dev/null || echo "not installed")
_lark_ver=$(lark-cli --version 2>/dev/null | head -1 || echo "not installed")
_ef_ver=$(eigenflux version 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cli_version', d.get('version','?')))" 2>/dev/null || echo "not installed")
echo "Claude: $_claude_ver"
echo "Lark CLI: $_lark_ver"
echo "EigenFlux: $_ef_ver"
