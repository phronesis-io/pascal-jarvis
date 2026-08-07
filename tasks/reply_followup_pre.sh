#!/usr/bin/env bash
# Pre-hook: claim the oldest un-answered suggested-reply tap and emit the
# card + the tapped sentence for the model to act on.
# Empty output = nothing queued, the heartbeat skips the task entirely.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$JARVIS_DIR${PYTHONPATH:+:$PYTHONPATH}"

python3 - <<'PYEOF' 2>/dev/null || true
from core import memorial

# Bounded loop: skip requests the conversation already answered (Pascal
# spoke before this task ran, so bot.sh consumed the decision injection and
# the session acted on it — answering again here would be a double response).
for _ in range(5):
    req = memorial.reply_followup_claim()
    if not req:
        break
    mid = str(req.get("memorial_id", ""))
    st = memorial.get_memorial(mid)
    if st is None:
        memorial.reply_followup_complete(mid)
        continue
    conv_key = st.get("chat_id", "") or memorial._resolve_user_id()
    if conv_key and not memorial._injection_queued(
            conv_key, f"memorial-decision:{mid}"):
        memorial.reply_followup_complete(mid)
        continue
    print(f"[reply-followup {st['id']}] 来源: {st.get('source', '?')}")
    print(f"卡片标题: {st.get('title', '')}")
    print(f"他点的按钮（当作他亲口说的一句话）: 「{req.get('label', '')}」")
    print(f"点击时间: {req.get('ts', '')}")
    print("卡片原文:")
    print(str(st.get("body", ""))[:1500])
    break
PYEOF
