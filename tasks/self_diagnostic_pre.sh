#!/usr/bin/env bash
# Pre-hook: collect system health data for self-diagnostic
WORK_DIR="${WORK_DIR:-$(cd "$JARVIS_DIR/.." 2>/dev/null && pwd || echo "$JARVIS_DIR")}"
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.claude/projects/-Users-pascal-Desktop-jarvis/memory}"

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
elif [ "$_stream_count" -eq 0 ]; then
  echo "⚠️ Stream NOT running — real-time messages will not be received"
else
  echo "⚠️ $_stream_count stream processes found — competing connections cause 'Connection replaced' loop"
fi

# 7. Channel watermarks (REQ-12) — flags starved tasks / open circuits /
#    delivery failures so dead channels are caught here, not by the user.
echo ""
(cd "$JARVIS_DIR" && JARVIS_DIR="$JARVIS_DIR" python3 -m core.watermarks 2>/dev/null) \
  || echo "--- Channel Watermarks ---
  ⚠️ watermark check itself failed to run"

# 8. CLI versions
echo ""
echo "--- CLI Versions ---"
_claude_ver=$(claude --version 2>/dev/null || echo "not installed")
_lark_ver=$(lark-cli --version 2>/dev/null | head -1 || echo "not installed")
_ef_ver=$(eigenflux version 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cli_version', d.get('version','?')))" 2>/dev/null || echo "not installed")
echo "Claude: $_claude_ver"
echo "Lark CLI: $_lark_ver"
echo "EigenFlux: $_ef_ver"
