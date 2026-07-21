#!/usr/bin/env bash
# Pre-hook: morning anchor (REQ-115) — one structured nudge around 8:30.
#
# Gates (empty output → heartbeat skips, established contract):
#   1. Window 8:30-10:00 (heartbeat cycles are not exact, so the window gives
#      the 30m interval a few shots; the daily stamp keeps it to ONE send).
#   2. At most once per day: data/morning_anchor_state.json is stamped by
#      morning_anchor_post.py right before the card is emitted — checking the
#      stamp here and writing it there means a failed Claude call retries
#      within the window while a sent card can never double-fire.
#
# The anchor ITEMS are personal → gitignored data/morning_anchor_personal.txt
# (one per line), neutral defaults otherwise (multi-user product principle).
# Pascal hates nagging: the nudge is ONE short line, no follow-up if ignored.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
export PYTHONPATH="$JARVIS_DIR${PYTHONPATH:+:$PYTHONPATH}"

# 10# forces base-10 so "08"/"09" aren't parsed as invalid octal in (( ))
hour=$((10#$(date +%H)))
minute=$((10#$(date +%M)))
hm=$((hour * 60 + minute))
# 510 = 8:30, 600 = 10:00
if [ "$hm" -lt 510 ] || [ "$hm" -ge 600 ]; then
  exit 0
fi

# Daily dedup: stamp is written by the post-script when the card actually goes out.
status=$(python3 -m core.lifelog anchor-status 2>/dev/null || echo due)
if [ "$status" = "sent" ]; then
  exit 0
fi

echo "Current time: $(date '+%H:%M') ($(date '+%A, %Y-%m-%d'))"
echo ""
echo "=== TODAY'S ANCHOR ITEMS (from per-user config, or neutral defaults) ==="
python3 -m core.lifelog anchor-items 2>/dev/null || true
echo ""

# Morning calendar context — if a matching morning routine event already sits
# on the calendar, the prompt still sends one line (per REQ-115), it just
# reads the situation instead of ignoring it.
calendar_file="$MEMORY_DIR/hot/calendar_today.md"
if [ -f "$calendar_file" ]; then
  echo "=== TODAY'S CALENDAR (context only) ==="
  head -15 "$calendar_file" 2>/dev/null || true
fi
