#!/usr/bin/env bash
# Pre-hook: claim the oldest 「看不懂」request and emit the card for retelling.
# Empty output = nothing queued, the heartbeat skips the task entirely.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$JARVIS_DIR${PYTHONPATH:+:$PYTHONPATH}"

python3 - <<'PYEOF' 2>/dev/null || true
from core import memorial

req = memorial.explain_claim()
if req:
    st = memorial.get_memorial(str(req.get("memorial_id", "")))
    if st is None:
        memorial.explain_complete(str(req.get("memorial_id", "")))
    else:
        print(f"[explain {st['id']}] 来源: {st.get('source', '?')}")
        print(f"原标题: {st.get('title', '')}")
        print("原文:")
        print(str(st.get("body", ""))[:1500])
PYEOF
