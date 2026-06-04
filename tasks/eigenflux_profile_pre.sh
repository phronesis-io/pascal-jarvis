#!/usr/bin/env bash
# Pre-hook: get current EigenFlux profile via CLI.
#
# Off-peak refresh: the upstream plugin (profile-refresher.ts) refreshes the
# profile once a day at a random 1–5 AM local time, off the network's peak.
# We approximate that cheaply: the task is evaluated hourly but only proceeds
# when (a) it's the 1–5 AM window AND it hasn't refreshed in ~20h, OR (b) the
# profile is overdue (>25h) — the overdue branch guarantees a not-always-on bot
# (e.g. a laptop asleep overnight) still refreshes instead of stalling forever.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=../plugins/eigenflux/client.sh
. "$JARVIS_DIR/plugins/eigenflux/client.sh"

eigenflux_require || exit 0

STATE="$JARVIS_DIR/eigenflux/.profile_refresh_state"
now=$(date +%s)
hour=$(date +%-H 2>/dev/null || date +%H | sed 's/^0//')
last=$(cat "$STATE" 2>/dev/null || echo 0)
case "$last" in ''|*[!0-9]*) last=0 ;; esac
age=$(( now - last ))

in_window=false
[ "$hour" -ge 1 ] && [ "$hour" -lt 5 ] && in_window=true

# 72000s = 20h (don't double-run within a day); 90000s = 25h (overdue safety).
if [ "$in_window" = true ] && [ "$age" -ge 72000 ]; then
  :
elif [ "$age" -ge 90000 ]; then
  :
else
  exit 0  # off-window and not overdue — no-op this cycle
fi

current=$(eigenflux_profile_show)
[ -z "$current" ] && exit 0

# Mark this refresh so the gate holds for the rest of the day.
echo "$now" > "$STATE" 2>/dev/null || true

echo "Current EigenFlux profile:"
echo "$current"
