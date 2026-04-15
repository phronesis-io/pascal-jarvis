#!/usr/bin/env bash
# Pre-hook: fetch unread EigenFlux messages (auto-persisted by client)
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

python3 -c "
import sys; sys.path.insert(0, '$JARVIS_DIR')
from plugins.eigenflux.client import EigenFluxClient
import json
client = EigenFluxClient('$JARVIS_DIR/eigenflux')
result = client.fetch_messages()  # messages auto-persisted
messages = result.get('data', {}).get('messages', [])
if messages:
    for m in messages:
        print(json.dumps(m))
" 2>/dev/null || true
