#!/usr/bin/env bash
# Pre-hook: claim the oldest un-answered suggested-reply tap and emit the
# card + the tapped sentence for the model to act on.
# Empty output = nothing queued, the heartbeat skips the task entirely.
JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$JARVIS_DIR${PYTHONPATH:+:$PYTHONPATH}"

# stderr is NOT swallowed: an import error or deploy drift here would
# otherwise wedge the queue with zero signal.
python3 - <<'PYEOF' || true
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
    # First claim only: a consumed injection still carried the armed
    # 「照它行动」wording, so the conversation really did handle the tap.
    # On a retake the injection was already defused below — its consumption
    # proves nothing, and dropping the tap would resurrect the dead end.
    conv_key = st.get("chat_id", "") or memorial._resolve_user_id()
    if (int(req.get("attempts") or 1) <= 1 and conv_key
            and not memorial._injection_queued(
                conv_key, f"memorial-decision:{mid}")):
        memorial.reply_followup_complete(mid)
        continue
    # Defuse the injection NOW, not after the model answers: the model call
    # is minutes long, the tap's toast invites him to reply, and one reply
    # would make the conversation execute the still-armed「照它行动」while
    # we are answering it here — double action. If this model call dies the
    # retake re-answers; a defused injection is the cheaper failure.
    memorial.settle_decision_context(mid, (
        f"[奏折回复·接手中] 关于「{st.get('title', '')}」Pascal 点了"
        f"「{req.get('label', '')}」，后台已在处理并会另行答复。"
        "不要重复执行，如他问起就说正在办。"))
    print(f"[reply-followup {st['id']}] 来源: {st.get('source', '?')}")
    print(f"卡片标题: {st.get('title', '')}")
    print(f"他点的按钮（当作他亲口说的一句话）: 「{req.get('label', '')}」")
    print(f"点击时间: {req.get('ts', '')}")
    print("卡片原文:")
    print(str(st.get("body", ""))[:1500])
    break
PYEOF
