#!/usr/bin/env bash
# Pre-hook: detect transition moments + inject context for value-driven check-ins.
#
# Design principle (CHI 2025): interrupt at TRANSITIONS (meeting just ended,
# focus block completed), not during idle time. Idle ≠ bored; idle may = thinking.
#
# - Only triggers during waking hours (9:00-22:00)
# - Detects transition context from calendar (meeting just ended? big gap ahead?)
# - Reads ALL past check-ins: older ones as topic blocklist, recent 3 as full text
# - Rotates through value-oriented "modes" by hour

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
if ! PYTHONPATH="$JARVIS_DIR" JARVIS_DIR="$JARVIS_DIR" \
    python3 -m core.retained_rhythms enabled checkin >/dev/null 2>&1; then
  exit 0
fi
eval $(bash "$JARVIS_DIR/scripts/config_env.sh" 2>/dev/null) || true

hour=$((10#$(date +%H)))  # 10# forces base-10 so "08"/"09" aren't parsed as invalid octal in (( ))
if [ "$hour" -lt "${WORK_START:-9}" ] || [ "$hour" -ge "${WORK_END:-22}" ]; then
  exit 0
fi

now_ts=$(date '+%H:%M')
day=$(date '+%A')
date_ymd=$(date '+%Y-%m-%d')

# Rate limit: max 2 checkins per day (7/21 乱联系诊断 — at observed quality,
# 2/day is generous; the relevance gate in the prompt should HEARTBEAT_OK
# most rounds anyway)
log_file="${MEMORY_DIR:-$HOME/.jarvis/memory}/system/checkin_log.jsonl"
if [ -f "$log_file" ]; then
  # ts values are "YYYY-MM-DD HH:MM" — a closing quote right after the date
  # never matches, which made the old cap a no-op (red-team 7/21 finding 5)
  today_count=$(grep -c "\"ts\": \"$date_ymd" "$log_file" 2>/dev/null || true)
  if [ "$today_count" -ge 2 ]; then
    exit 0
  fi
fi

# Do not call calendar/freebusy after deterministic cadence has already spent
# today's allowance. The model-facing brief is still generated below when a
# slot exists, but a closed budget now exits before any network request.
if ! JARVIS_DIR="$JARVIS_DIR" python3 -m core.companion preflight \
    >/dev/null 2>&1; then
  exit 0
fi

# Time-of-day flavor — rough buckets
if [ "$hour" -lt 12 ]; then
  phase="morning"
elif [ "$hour" -lt 14 ]; then
  phase="midday"
elif [ "$hour" -lt 18 ]; then
  phase="afternoon"
elif [ "$hour" -lt 20 ]; then
  phase="early-evening"
else
  phase="late-evening"
fi

# (7/21) hour-parity mode selection removed — the 2h cadence pinned it to
# wellbeing mode 88% of the time, producing the repetitive body-state cards
# the owner complained about 4 times. The prompt now uses a relevance gate.

# ── Calendar context: transition detection + next-event lookahead ──
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

# Recurring-appointment prep (e.g. a weekly session to prepare for). The
# schedule is personal, so it lives in the gitignored data/checkin_personal.sh
# which may set $therapy_prep using $day/$hour. Absent file = no prep block.
therapy_prep=""
if [ -f "$JARVIS_DIR/data/checkin_personal.sh" ]; then
  # shellcheck source=/dev/null
  . "$JARVIS_DIR/data/checkin_personal.sh"
fi
lark_plugin="$JARVIS_DIR/plugins/lark/client.sh"
transition_context=""

if [ -f "$lark_plugin" ] && command -v lark-cli &>/dev/null; then
  # shellcheck source=../plugins/lark/client.sh
  . "$lark_plugin"

  # Look back 1h and forward 2h to detect transitions
  # Use Beijing time with +08:00 offset (not UTC) to match local calendar
  past_iso="$(TZ=Asia/Shanghai date -v-1H +%Y-%m-%dT%H:%M:%S+08:00 2>/dev/null || TZ=Asia/Shanghai date -d '-1 hour' +%Y-%m-%dT%H:%M:%S+08:00)"
  now_iso="$(TZ=Asia/Shanghai date +%Y-%m-%dT%H:%M:%S+08:00)"
  future_iso="$(TZ=Asia/Shanghai date -v+2H +%Y-%m-%dT%H:%M:%S+08:00 2>/dev/null || TZ=Asia/Shanghai date -d '+2 hours' +%Y-%m-%dT%H:%M:%S+08:00)"

  # Get events in the [-1h, +2h] window
  freebusy=$(lark_freebusy "$past_iso" "$future_iso")

  # Filter/transition logic lives in checkin_busy_filter.py: all-day/multi-day
  # events (trips) must not read as BUSY, and that needed unit tests.
  transition_context=$(echo "$freebusy" | python3 "$JARVIS_DIR/tasks/checkin_busy_filter.py" 2>/dev/null || echo "calendar_unavailable")

  # If calendar says busy, skip
  if [ "$transition_context" = "BUSY" ]; then
    exit 0
  fi
fi

# ALL past check-ins — compressed to topic signatures to prevent repetition.
# Full text of last 3 for style awareness; older ones as topic-only blocklist.
log_file="${MEMORY_DIR:-$HOME/.jarvis/memory}/system/checkin_log.jsonl"
recent_checkins=""
if [ -f "$log_file" ]; then
  recent_checkins=$(LOG_FILE="$log_file" python3 -c "
import json, os, re

entries = []
path = os.environ['LOG_FILE']
try:
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
except OSError:
    pass

if not entries:
    exit(0)

# Older entries: topic keywords only (compact blocklist)
older = entries[:-3] if len(entries) > 3 else []
recent = entries[-3:] if len(entries) > 3 else entries

if older:
    print('=== USED TOPICS (DO NOT REPEAT these subjects) ===')
    for e in older:
        ts = e.get('ts', '')
        topics = e.get('topics', '')
        if not topics:
            # fallback: first line of content, truncated
            first_line = e.get('content', '').split(chr(10))[0][:80]
            topics = first_line
        print(f'[{ts}] {topics}')
    print()

print('=== RECENT CHECK-INS (full text — avoid similar topics, structure, openers) ===')
for e in recent:
    print(f\"[{e.get('ts','')}]\")
    print(e.get('content','').strip())
    print()
" 2>/dev/null || true)
fi

# Private config is the active profile source.  Do not resurrect an archived
# warm/interests.md merely because an old prompt still knows that filename.
interests=$(python3 -m core.triage_profile 2>/dev/null || true)

# ── Activity evidence (2026-07-13 feedback_idle_detection_signal) ──
# "No commits + empty calendar" ≠ idle: strategy work, discussions and
# external threads leave no commit trail, and a checkin that reads a quiet
# calendar as "idle" gets corrected by the user (observed 2026-07-13). Give
# the model ACTUAL interaction volume so it never infers idleness from
# absence alone.
# LIVE sources only. data/conversation_audit.db was rejected here: it is
# ingested once daily (~04:20), so a 12h query returns 0 on a busy evening —
# which would reproduce the exact misjudgment this block prevents, with
# hard-rule authority (2026-07-14 red-team catch).
activity_evidence=""
last_msg_file="/tmp/jarvis-last-msg"   # bot dedup file; mtime = last inbound msg
if [ -f "$last_msg_file" ]; then
  last_epoch=$(stat -f %m "$last_msg_file" 2>/dev/null || stat -c %Y "$last_msg_file" 2>/dev/null)
  if [ -n "$last_epoch" ]; then
    mins_ago=$(( ($(date +%s) - last_epoch) / 60 ))
    activity_evidence="- 最近一次消息互动: ${mins_ago} 分钟前"
  fi
fi
if [ -f "$JARVIS_DIR/jarvis.log" ]; then
  replies_today=$(grep "^\[$date_ymd" "$JARVIS_DIR/jarvis.log" 2>/dev/null | grep -c "Quote reply" || true)
  if [ -n "$replies_today" ] && [ "$replies_today" -gt 0 ] 2>/dev/null; then
    activity_evidence="$activity_evidence
- 今天对话往来（bot 回复计数）: ${replies_today} 轮"
  fi
fi
eng_log="$JARVIS_DIR/engagement_log.jsonl"
if [ -f "$eng_log" ]; then
  eng_today=$(grep "\"$date_ymd" "$eng_log" 2>/dev/null | grep -cv '"type": *"sent"' || true)
  if [ -n "$eng_today" ] && [ "$eng_today" -gt 0 ] 2>/dev/null; then
    activity_evidence="$activity_evidence
- 今天奏折/卡片互动（已读、批示、聊聊）: ${eng_today} 次"
  fi
fi
if [ -z "$activity_evidence" ]; then
  activity_evidence="(活动信号采集不可用——绝不能据此判断他今天闲/没干活)"
fi

# Engagement-driven content-mix steering (advisory, not a hard rule). Written
# by engagement-analyze post-hook; keeps checkins evolving from measured
# response patterns without letting the analyzer mutate prompts directly.
content_mix_file="${MEMORY_DIR:-$HOME/.jarvis/memory}/system/engagement_content_mix.md"
content_mix=""
if [ -f "$content_mix_file" ]; then
  content_mix=$(head -80 "$content_mix_file" 2>/dev/null || true)
fi

# ── Weather context (REQ-113) ──
# Empty when geo.amap_key isn't configured (core.weather context prints
# nothing, exit 0) — the heredoc line below then stays blank, a no-op.
weather_context=""
_wline=$(cd "$JARVIS_DIR" && python3 -m core.weather context 2>/dev/null || true)
if [ -n "$_wline" ]; then
  weather_context="Weather (use for outdoor-activity suggestions like 游泳/篮球): $_wline"
fi

# ── Relevance-gate inputs (7/21 乱联系根修) ──
# (a) what he actually said/did in the last 24h — the only legit hook for a
# checkin that isn't a standing request. Source: timeline/hourly_log.md
# (memory-hourly's digest of REAL interactions). The conversation_audit_*
# files were rejected here (red-team 7/21 finding 4): they are internal
# audit PRD reports, not what the owner said, and the 1h file is stale.
recent_conversation=""
_hourly_log="${MEMORY_DIR:-$HOME/.jarvis/memory}/timeline/hourly_log.md"
if [ -f "$_hourly_log" ]; then
  recent_conversation=$(tail -80 "$_hourly_log" 2>/dev/null)
fi
[ -z "$(echo "$recent_conversation" | tr -d '[:space:]')" ] && \
  recent_conversation="(无最近对话记录——锚点(a)不可用，只剩 standing requests 或 HEARTBEAT_OK)"

# (b) standing requests he explicitly made (per-user, gitignored)
standing_requests=""
if [ -f "$JARVIS_DIR/data/standing_requests_personal.txt" ]; then
  standing_requests=$(cat "$JARVIS_DIR/data/standing_requests_personal.txt" 2>/dev/null)
fi
[ -z "$standing_requests" ] && standing_requests="(none on file)"

cat <<EOF
Current time: $now_ts ($day, $date_ymd) — $phase
$therapy_prep
$weather_context

His last-24h conversation digest (anchor source (a) — a checkin must quote a
concrete follow-up from here, or match a standing request below, or HEARTBEAT_OK):
$recent_conversation

Standing requests he explicitly made (anchor source (b)):
$standing_requests

Calendar context:
$transition_context

Activity evidence (硬规则：判断他今天忙不忙时，以下面的实际互动量为准；
若信号缺失或为零，只说明采集面窄，不构成「他闲着」的证据。
「没有 commit + 日历空」≠ 闲着——战略思考/讨论/接外部线这类最值钱的活不留痕，
绝不能因为看不到留痕就说他今天闲/没干活):
$activity_evidence

User interests (for relevant knowledge nuggets):
$interests

Engagement-derived content mix steering (advisory; do not force it if context disagrees):
$content_mix

Past check-ins (MUST avoid repeating topics, openers, or structure):
$recent_checkins

Optional structured output — DIET line (REQ-114):
If (and ONLY if) the user EXPLICITLY stated in today's conversation/memory
what they ate (their own words, not your inference), append ONE final line:
DIET: 餐次|食物1、食物2[|备注]
餐次 must be one of 早/午/晚/加餐. Example: DIET: 午|牛肉面、青菜
This line is stripped before the card is sent — it never reaches the user.
No clear first-person food mention today = do NOT emit the line. Never invent items.
EOF

cat <<'EOF'

Mandatory structured output — THEMES line (7/21 含义级去重):
If you send a checkin (not HEARTBEAT_OK), the LAST line must be:
THEMES: 概念1, 概念2[, 概念3, 概念4]
2-4 meaning-level concept tags for what this checkin is ABOUT (e.g.
"康复打卡, 前锯肌" or "增长数据跟进"). Used to block repeats by meaning —
be honest and general; if the true theme matches a past checkin's theme,
don't send at all. The line is stripped before the card reaches the user.
EOF

# ── companion budget (2026-08-02) ────────────────────────────────────────────
# Cadence is decided by deterministic code, not by the prompt. Leaving "how
# often should I speak" to the model produced both the 7/22 card storm and the
# 10-day silence that followed the 7/21 rewrite: the same instruction ("stay
# quiet unless it's worth it") swung to both extremes because nothing measured
# the result. core.companion allocates a per-kind daily budget from the
# ledger's own evidence and, after FLOOR_HOURS of muteness, states that a card
# is owed. The model chooses WHAT to say; it no longer chooses whether silence
# is acceptable.
# The KIND taxonomy and output contract ride inside the brief itself —
# core.companion.KIND_HELP is the single definition of the unit of learning,
# not a second copy in shell.
python3 -m core.companion brief 2>/dev/null || true
