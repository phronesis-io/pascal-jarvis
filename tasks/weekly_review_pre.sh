#!/usr/bin/env bash
# Pre-hook: build the deterministic Matter result review.
# Only runs Sunday 10:00-12:00.
set -euo pipefail

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

_python="$JARVIS_DIR/scripts/python.sh"
if [ ! -x "$_python" ]; then
  _python=python3
fi
_local_now=$(cd "$JARVIS_DIR" && "$_python" -c \
  'from core.timeutil import now_local; print(now_local().strftime("%u %H %Y-W%V"))')
read -r dow hour _this_week <<<"$_local_now"  # 7 = Sunday
if [ "$dow" -ne 7 ] || [ "$hour" -lt 10 ] || [ "$hour" -ge 12 ]; then
  exit 0
fi

# Dedup: skip if already succeeded this week (prevents double-fire on restart).
_stamp="$JARVIS_DIR/data/.weekly_review_stamp"
if [ -f "$_stamp" ] && [ "$(cat "$_stamp" 2>/dev/null)" = "$_this_week" ]; then
  exit 0
fi

cd "$JARVIS_DIR"
"$_python" -c 'from core.matter_runs import recover_expired_runs; recover_expired_runs()'
exec "$_python" -m core.matter_review --days 7 --limit 8
