#!/usr/bin/env bash
# Run the Python interpreter selected by Jarvis setup/runtime policy.
set -euo pipefail

JARVIS_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
export JARVIS_DIR
# shellcheck source=runtime_env.sh
source "$JARVIS_DIR/scripts/runtime_env.sh"
exec "$JARVIS_PYTHON" "$@"
