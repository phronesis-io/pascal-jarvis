#!/usr/bin/env bash
# Pre-hook: collect system health data for self-diagnostic
# JARVIS_DIR must be resolved BEFORE WORK_DIR derives from it: the old order
# expanded an unset $JARVIS_DIR, so `cd "/.."` succeeded into `/` and every
# standalone run reported WORK_DIR=/ — an empty repo scan ($WORK_DIR/repos)
# and a memory slug of "-" (0 hot / 0 warm / rules ✗). Exported by bot.sh in
# production, which is why it only ever showed up on manual runs.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK_DIR="${WORK_DIR:-$(cd "$JARVIS_DIR/.." 2>/dev/null && pwd || echo "$JARVIS_DIR")}"
_CODE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
_TO=(python3 "$_CODE_DIR/scripts/run_with_timeout.py" 30)
# The tiered memory belongs to the agent runtime directory, so its Claude
# project slug derives from JARVIS_DIR — not from WORK_DIR, whose slug points
# at a different (flat) memory directory.
_mem_slug=$(python3 -c "from pathlib import Path; print(str(Path('$JARVIS_DIR').resolve()).replace('/','-').replace('.','-'))")
MEMORY_DIR="${MEMORY_DIR:-$HOME/.claude/projects/$_mem_slug/memory}"

# REQ-39: leave a copy of this report for the deterministic alert post-script
# (tasks/self_diagnostic_post.py scans it for ⚠️ lines — detection and
# delivery are split so SILENT_TASKS can never mute a real alarm again).
exec > >(tee "$JARVIS_DIR/.diag_last_pre.txt")

# Optional-feature switches (2026-07-13 fresh-install audit): a collaborator's
# default install got ⚠️ lines every 4h for features they never enabled
# (EigenFlux, Lark user calendar, Pascal's personal site). Every ⚠️ below must
# be gated on the feature actually being configured.
_HAS_EF=0; command -v eigenflux >/dev/null 2>&1 && _HAS_EF=1
_LARK_UID=$(python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from core.config import Config
print(Config('$JARVIS_DIR/jarvis.yaml').lark.get('user_id', '') or '')" 2>/dev/null)
_SITE_DIR=$(python3 -c "
import sys, os; sys.path.insert(0, '$JARVIS_DIR')
from core.config import Config
v = Config('$JARVIS_DIR/jarvis.yaml').get('personal_site.repo_dir') or ''
print(os.path.expanduser(v))" 2>/dev/null)

echo "=== SYSTEM HEALTH CHECK ==="
echo "Time: $(date '+%Y-%m-%d %H:%M %A')"
echo ""

# 1. EigenFlux profile staleness
echo "--- EigenFlux Profile ---"
if [ "$_HAS_EF" -eq 1 ]; then
  profile_ts=$(eigenflux profile show 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
ts = d.get('profile', {}).get('updated_at', 0) / 1000
from datetime import datetime
print(datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M'))
" 2>/dev/null || echo "unknown")
  echo "Last updated: $profile_ts"
else
  echo "(eigenflux CLI not installed — skipped)"
fi

# 2. Calendar freshness (needs a configured Lark user)
echo ""
echo "--- Calendar ---"
if [ -z "$_LARK_UID" ]; then
  echo "(lark.user_id not configured — skipped)"
else
  cal_file="$MEMORY_DIR/calendar_today.md"
  [ ! -f "$cal_file" ] && cal_file="$MEMORY_DIR/hot/calendar_today.md"
  if [ -f "$cal_file" ]; then
    cal_sync=$(grep -o 'synced [0-9-]* [0-9:]*' "$cal_file" | head -1)
    echo "Last sync: $cal_sync"
  else
    echo "⚠️ 日历快照文件一直没生成过——「日历同步」可能从没跑成"
  fi
fi

# 2b. Calendar user-token probe (REQ-83): calendar-sync fetches --as user;
#     when the user token lapses (6/29-30: ×7) the sync degrades to a stale
#     snapshot. Probe a 1h agenda window with the same identity so token
#     death pages HERE, deterministically — doctor.sh only probes the bot
#     identity. (_GATE_TRIGGERS in the post redacts auth failure details.)
if [ -n "$_LARK_UID" ] && command -v lark-cli >/dev/null 2>&1; then
  _user_probe_err="$JARVIS_DIR/tmp/.lark_user_probe_err"
  mkdir -p "$JARVIS_DIR/tmp"
  : > "$_user_probe_err"
  _probe_start=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
  _probe_end=$(python3 -c "from datetime import datetime,timezone,timedelta; print((datetime.now(timezone.utc)+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
  if "${_TO[@]}" lark-cli calendar +agenda --as user --format json \
       --start "$_probe_start" --end "$_probe_end" >/dev/null 2>"$_user_probe_err"; then
    echo "User-token probe: ✓"
  elif grep -Eqi 'keychain (Get failed|access blocked)|credential manager.*(locked|accessible)' "$_user_probe_err"; then
    echo "⚠️ 飞书后台 user 凭证暂不可读 — 日历/邮件等使用最后成功快照；这是运行环境问题，不需要重复授权，系统会继续自动重试"
  elif grep -Eqi 'token.*(expired|invalid)|failed to authenticate|HTTP (401|403)|Request not allowed' "$_user_probe_err"; then
    echo "⚠️ 日历 user token 探针失败 — 日历/邮件等信道只能用旧快照兜底，点「现在授权」我发你授权链接一键修复"
  else
    echo "⚠️ 飞书 user 数据通道暂不可用 — 日历/邮件等使用最后成功快照，系统会继续自动重试"
  fi
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

# 4. Personal site (per-user config: jarvis.yaml personal_site.repo_dir —
#    2026-07-13: the repo name was hardcoded here, so every non-owner install
#    alarmed "⚠️ Repo not found" forever, with no subject in the message)
if [ -n "$_SITE_DIR" ]; then
  echo ""
  echo "--- Personal Site ---"
  if [ -d "$_SITE_DIR" ]; then
    last_commit=$(git -C "$_SITE_DIR" log -1 --format="%ci %s" 2>/dev/null || echo "unknown")
    echo "Last commit: $last_commit"
  else
    echo "⚠️ personal-site 仓库不存在：$_SITE_DIR（jarvis.yaml personal_site.repo_dir 指向的目录）"
  fi
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
if [ "$_HAS_EF" -ne 1 ]; then
  echo "(eigenflux CLI not installed — skipped)"
else
# Match only processes whose command starts with "eigenflux stream"
# (avoids matching Claude prompts that mention "eigenflux stream" in text)
_stream_pids=$(ps -eo pid,comm,args | awk '$2 == "eigenflux" && $3 ~ /eigenflux/ && $4 == "stream" {print $1}')
# grep -c prints the count ITSELF even on no match (exit 1) — the old
# `|| echo 0` produced "0\n0", both -eq tests errored out, and a dead stream
# printed a bare "⚠️ 0" instead of "Stream NOT running" (2026-07-13 audit,
# the least decodable line on the collaborator's first-install alert card).
_stream_count=$(echo "$_stream_pids" | grep -c '[0-9]')
if [ "$_stream_count" -eq 1 ]; then
  _stream_pid=$(echo "$_stream_pids" | head -1)
  _stream_uptime=$(ps -p "$_stream_pid" -o etime= 2>/dev/null | tr -d ' ')
  echo "✓ Stream running (PID $_stream_pid, uptime $_stream_uptime)"
  # Process alive ≠ connected: on 2026-06-11 the stream retried 'Connect
  # failed: EOF' for an extended outage while this check showed green.
  _recent_fails=$(tail -50 "$JARVIS_DIR/jarvis.log" 2>/dev/null | grep -c 'Connect failed' || true)
  _recent_ok=$(tail -50 "$JARVIS_DIR/jarvis.log" 2>/dev/null | grep -c 'Connected\. Streaming' || true)
  if [ "${_recent_fails:-0}" -ge 3 ] && [ "${_recent_ok:-0}" -eq 0 ]; then
    echo "⚠️ EigenFlux 实时连接一直连不上（进程还在，最近 ${_recent_fails} 次重连全失败）——实时消息进不来，多半是对方服务端的问题"
  fi
elif [ "$_stream_count" -eq 0 ]; then
  # No CLI child ≠ outage: the supervising loop (core.ef_stream_loop) kills
  # and respawns the child through backoff windows (stall-kill, deploy
  # restart, reconnect) — sampling inside such a window produced recurring
  # false "Stream NOT running" alerts (Pascal 2026-07-14; also fired on the
  # collaborator's first install). Loop alive = supervised, it WILL respawn;
  # only a dead loop is a real outage (components.yaml pgrep also catches it).
  # Anchored ps match (NOT bare pgrep -f substring — the 2026-07-07 phantom
  # crash loop lesson): only a python interpreter running `-m core.ef_stream_loop`
  # counts, never an editor/claude prompt that mentions the string.
  _loop_alive=$(ps -eo args | grep -Ec '^[^ ]*[Pp]ython[^ ]* -m core\.ef_stream_loop' || true)
  if [ "${_loop_alive:-0}" -ge 1 ]; then
    echo "✓ Stream between connections (supervisor alive — reconnect/restart window, self-heals)"
  else
    echo "⚠️ EigenFlux 实时接收没在运行——实时消息收不到了"
  fi
else
  echo "⚠️ 发现 $_stream_count 个 EigenFlux 实时连接在同时抢线——会互相顶掉，得收敛成一个"
fi
fi  # _HAS_EF

# 7a. Card callback support watch: lark-cli 1.0.44 can't consume
#     card.action.trigger (larksuite/cli#1051) — callback buttons are disabled
#     across the product. Flag loudly the moment an upgrade adds support so
#     they can be re-enabled (PRD REQ-17).
if "${_TO[@]}" lark-cli event list 2>/dev/null | grep -qi 'card'; then
  echo ""
  echo "🎉 lark-cli now lists card events — card callback buttons can be re-enabled!"
  echo "   (re-add the 收藏 button in tasks/content_recommend_post.py, add card.action.trigger consumption; see PRD REQ-17)"
fi

# 6b. Presence floor (2026-08-07) — the 7/24 cliff ran ten days with every
#     check green because cards were "delivered" to surfaces nobody opens.
#     Feishu arrival volume is the product's pulse; below floor = page.
echo ""
(cd "$JARVIS_DIR" && JARVIS_DIR="$JARVIS_DIR" python3 -m core.presence check 2>/dev/null) || true

# 7. Channel watermarks (REQ-12) — flags starved tasks / open circuits /
#    delivery failures so dead channels are caught here, not by the user.
echo ""
(cd "$JARVIS_DIR" && JARVIS_DIR="$JARVIS_DIR" python3 -m core.watermarks 2>/dev/null) \
  || echo "--- Channel Watermarks ---
  ⚠️ 后台任务的健康检查这一步自己没跑成"

# The heartbeat entry for self-improve is intentionally empty: it only starts
# a detached coding session. Check that session's acquire/run/release receipt,
# not the scheduler's expected empty stdout, and surface only after bounded
# automatic retries have already failed.
(cd "$JARVIS_DIR" && JARVIS_DIR="$JARVIS_DIR" \
  python3 -m core.self_improve_cycle health 2>/dev/null) || true

# 7b. Component manifest (REQ-40): the single source of truth for "what
#     should be running". Failures print as ⚠️ lines → REQ-39 alert path.
echo ""
(cd "$JARVIS_DIR" && python3 -m core.components 2>/dev/null) \
  || true  # non-zero exit = failing components; the ⚠️ lines carry the signal

# 7c.1 Resident resource headroom. The 2026-07-24 heartbeat accumulated
# SQLite descriptors until [Errno 24] interrupted live conversations.
echo ""
echo "--- Resident Resources ---"
_heartbeat_pid=$(ps -eo pid,args | awk \
  '/[Pp]ython[^ ]* -m core\.heartbeat_loop/ {print $1; exit}')
if [ -n "$_heartbeat_pid" ]; then
  (cd "$JARVIS_DIR" && python3 -m core.resource_health \
    --pid "$_heartbeat_pid" 2>/dev/null) \
    || true
else
  echo "⚠️ 找不到心跳调度进程，这次没法检查它占用的资源"
fi

# 7c. Intent breach daily check (REQ-35): silently-expired commitments in the
#     last 24h mean the retry+breach pipeline itself is broken — page loudly.
echo ""
echo "--- Intent Lifecycle ---"
_dropped=$(sqlite3 "file:$JARVIS_DIR/data/jarvis.db?mode=ro" \
  "SELECT COUNT(*) FROM intentions WHERE status='expired' \
   AND (last_error LIKE 'auto-expired%' OR last_error LIKE '%expired after%attempts%') \
   AND triggered_at >= datetime('now','-1 day')" 2>/dev/null || echo "?")
if [ "$_dropped" != "?" ] && [ "${_dropped:-0}" -gt 0 ]; then
  echo "⚠️ 过去24小时有 $_dropped 个定时提醒重试多次仍失败、被放弃了——请核对补发卡片确实发出"
else
  echo "✓ No silently dropped intents in the last 24h"
fi

# 7d. Skip digest check (REQ-78.2): any intent_occurrence_skipped /
#     expires_at_lapsed event in the last 24h means the scheduler stalled past
#     occurrences — ⚠️ line feeds the REQ-39 deterministic alert path.
echo ""
echo "--- Skipped Occurrences (24h) ---"
(cd "$JARVIS_DIR" && python3 -m core.skip_digest --diag 2>/dev/null) \
  || echo "⚠️ 「被跳过的定时提醒」这项检查自己没跑成"

# 7e. Perception source health: a source can be enabled, scheduled, and
#     failing EVERY pass without anything noticing — the phronesis lark_chat
#     shadow failed 1154 consecutive collections over 13 days while its
#     "parity window" was believed to be running. Non-zero exit prints the
#     ⚠️ lines itself; `|| true` keeps set -e-free shells from swallowing them.
echo ""
(cd "$JARVIS_DIR" && JARVIS_DIR="$JARVIS_DIR" MEMORY_DIR="$MEMORY_DIR" \
  python3 -m core.perception --diag 2>/dev/null) \
  || true  # non-zero exit = stuck sources; the ⚠️ lines carry the signal

# 8. CLI versions
echo ""
echo "--- CLI Versions ---"
_claude_ver=$(claude --version 2>/dev/null || echo "not installed")
_lark_ver=$(lark-cli --version 2>/dev/null | head -1 || echo "not installed")
_ef_ver=$(eigenflux version 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cli_version', d.get('version','?')))" 2>/dev/null || echo "not installed")
echo "Claude: $_claude_ver"
echo "Lark CLI: $_lark_ver"
echo "EigenFlux: $_ef_ver"
