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
eval $(bash "$JARVIS_DIR/scripts/config_env.sh" 2>/dev/null) || true

hour=$((10#$(date +%H)))  # 10# forces base-10 so "08"/"09" aren't parsed as invalid octal in (( ))
if [ "$hour" -lt "${WORK_START:-9}" ] || [ "$hour" -ge "${WORK_END:-22}" ]; then
  exit 0
fi

now_ts=$(date '+%H:%M')
day=$(date '+%A')
date_ymd=$(date '+%Y-%m-%d')

# Rate limit: max 6 checkins per day (prevents spam on free days)
log_file="${MEMORY_DIR:-$HOME/.jarvis/memory}/system/checkin_log.jsonl"
if [ -f "$log_file" ]; then
  today_count=$(grep "\"$date_ymd\"" "$log_file" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$today_count" -ge 6 ]; then
    exit 0
  fi
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

# Two core modes — alternate by even/odd hour
if (( hour % 2 == 0 )); then
  mode="connection"
else
  mode="wellbeing"
fi

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

# Load interests for context-aware nuggets
interests_file="${MEMORY_DIR:-$HOME/.jarvis/memory}/warm/interests.md"
interests=""
if [ -f "$interests_file" ]; then
  interests=$(cat "$interests_file" 2>/dev/null)
fi

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

cat <<EOF
Current time: $now_ts ($day, $date_ymd) — $phase
Suggested mode this round: $mode
$therapy_prep

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
EOF
