#!/usr/bin/env bash
# Install/verify the launchd supervision config (REQ-40).
#
# The fixes that revived the dashboard and session-backup previously lived
# ONLY in ~/Library/LaunchAgents — a fresh install reproduced the 23-day
# dashboard outage. These plists are the versioned source of truth.
#
# TCC hard constraints on this Mac (memory: jarvis-launchd-tcc-gotchas):
#  1. StandardOut/ErrorPath must NOT point under ~/Desktop (launchd has no
#     Desktop TCC permission → exit 78, zero logs). Use /tmp/jarvis-*.log.
#  2. Interpreter must be /opt/homebrew/bin/python3 (system python lacks
#     deps AND Homebrew python carries the Desktop TCC grant).
#  3. bash scripts cannot be ProgramArguments directly (no Desktop TCC) —
#     wrap with python3 -c 'subprocess.call(["/bin/bash", script])'.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/LaunchAgents"
UID_N=$(id -u)

for plist in "$HERE"/com.*.plist; do
  name=$(basename "$plist")
  label="${name%.plist}"
  if ! cmp -s "$plist" "$DEST/$name" 2>/dev/null; then
    cp "$plist" "$DEST/$name"
    echo "installed $name"
    launchctl bootout "gui/$UID_N/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_N" "$DEST/$name"
    echo "  (re)bootstrapped $label"
  else
    echo "up-to-date $name"
  fi
  launchctl print "gui/$UID_N/$label" >/dev/null 2>&1 \
    && echo "  ✓ $label loaded" \
    || echo "  ⚠️ $label NOT loaded"
done
