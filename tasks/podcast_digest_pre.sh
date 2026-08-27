#!/usr/bin/env bash
# Pre-hook: daily podcast digest (Pascal 2026-08-27 —「每天给我搞一点，我简单
# 看看，别让我错过了」).
#
# Deterministic half only: find one unseen episode from the watchlist and pull
# its real captions to a local file. The task itself then READS that file and
# writes the digest — it never describes an episode it has not read (REQ-78).
#
# Gates (empty output → heartbeat skips the task entirely):
#   1. 07:00-10:00 window, so the digest is on his phone inside the 09:00-14:00
#      golden window rather than at 03:00 where nothing gets read.
#   2. Once per calendar day, stamped by podcast_digest_post.py right before
#      the card is emitted — a failed Claude call retries inside the window.
#   3. Nothing new on the watchlist → silence, not a "今天没有播客" card.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$JARVIS_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd "$JARVIS_DIR" || exit 0

hour=$((10#$(date +%H)))
if [ "$hour" -lt 7 ] || [ "$hour" -ge 10 ]; then
  exit 0
fi

today=$(date +%F)
stamp_file="$JARVIS_DIR/data/podcast_digest_day.txt"
if [ -f "$stamp_file" ] && [ "$(cat "$stamp_file" 2>/dev/null)" = "$today" ]; then
  exit 0
fi

evidence=$(python3 -m core.podcasts pick 2>/dev/null)
[ -z "$evidence" ] && exit 0

echo "$evidence"
