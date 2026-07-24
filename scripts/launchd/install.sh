#!/usr/bin/env bash
# Install/verify the launchd supervision config (REQ-40).
#
# The plist files in this directory are TEMPLATES with __JARVIS_DIR__,
# __WORK_DIR__, and __HOME__ placeholders. This script substitutes them
# with real paths at install time — no hardcoded user paths in tracked code.
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

JARVIS_DIR="$(cd "$HERE/../.." && pwd)"
WORK_DIR="${WORK_DIR:-$(cd "$JARVIS_DIR/../.." 2>/dev/null && pwd || echo "$JARVIS_DIR")}"

for plist in "$HERE"/com.*.plist; do
  name=$(basename "$plist")
  label="${name%.plist}"
  if [[ "$label" == "com.pascal.jarvis.taskline" \
        && ! -x "$WORK_DIR/repos/taskline/dist/taskline-server" ]]; then
    launchctl bootout "gui/$UID_N/$label" 2>/dev/null || true
    rm -f "$DEST/$name" "$DEST/$name.tmp"
    echo "skipped $name (optional Taskline binary not installed)"
    continue
  fi
  # Template substitution → installed copy
  sed \
    -e "s|__JARVIS_DIR__|$JARVIS_DIR|g" \
    -e "s|__WORK_DIR__|$WORK_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$plist" > "$DEST/$name.tmp"
  if ! cmp -s "$DEST/$name.tmp" "$DEST/$name" 2>/dev/null; then
    mv "$DEST/$name.tmp" "$DEST/$name"
    echo "installed $name"
    launchctl bootout "gui/$UID_N/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_N" "$DEST/$name"
    echo "  (re)bootstrapped $label"
  else
    rm "$DEST/$name.tmp"
    echo "up-to-date $name"
  fi
  launchctl print "gui/$UID_N/$label" >/dev/null 2>&1 \
    && echo "  ✓ $label loaded" \
    || echo "  ⚠️ $label NOT loaded"
done
