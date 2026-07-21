#!/usr/bin/env bash
# Pre-hook: weekly exercise summary card (REQ-116) — Sunday evening, once.
#
# Every run (any day) first harvests exercise-keyword events from the
# calendar cache into data/exercise_log.jsonl: hot/calendar_today.md only
# ever shows today + upcoming, so each day must be captured on the day
# itself or the Sunday summary would see an empty week. The harvest is
# idempotent (deduped on date+time+activity) and cheap (one md parse).
#
# Card gates (empty output → heartbeat skips):
#   1. Sunday 18:00-22:00 window (interval is 1h so the window gets several
#      shots — the 7d-interval weekly-review once missed its gate 26 days
#      straight, hence short interval + gate + stamp instead).
#   2. Once per ISO week via data/exercise_week_state.json; the stamp is
#      written by exercise_week_post.py right before the card is emitted, so
#      a failed Claude call retries within the window.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"
export PYTHONPATH="$JARVIS_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Rolling daily harvest — runs BEFORE the Sunday gate on purpose. Fail-open:
# a harvest error must never surface as task noise.
summary=$(python3 -m core.lifelog exercise-week --harvest 2>/dev/null || true)

dow=$(date +%u)   # 7 = Sunday
hour=$((10#$(date +%H)))
if [ "$dow" -ne 7 ] || [ "$hour" -lt 18 ] || [ "$hour" -ge 22 ]; then
  exit 0
fi

# Weekly dedup: stamp is written by the post-script when the card goes out.
status=$(python3 -m core.lifelog week-card-status 2>/dev/null || echo due)
if [ "$status" = "sent" ]; then
  exit 0
fi

if [ -z "$summary" ]; then
  exit 0
fi

echo "=== EXERCISE WEEK SUMMARY (last 7 days, aggregated) ==="
echo "$summary"
