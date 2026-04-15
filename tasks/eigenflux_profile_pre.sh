#!/usr/bin/env bash
# Pre-hook: get current EigenFlux profile
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

current=$(python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from plugins.eigenflux.client import EigenFluxClient
import json
client = EigenFluxClient('$JARVIS_DIR/eigenflux')
print(json.dumps(client.get_me(), indent=2))
" 2>/dev/null || true)

[ -z "$current" ] && exit 0
echo "Current EigenFlux profile:"
echo "$current"
