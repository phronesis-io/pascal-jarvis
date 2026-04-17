#!/usr/bin/env bash
# Pre-hook: get current EigenFlux profile via CLI
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=../plugins/eigenflux/client.sh
. "$JARVIS_DIR/plugins/eigenflux/client.sh"

eigenflux_require || exit 0

current=$(eigenflux_profile_show)
[ -z "$current" ] && exit 0

echo "Current EigenFlux profile:"
echo "$current"
