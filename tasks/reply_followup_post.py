#!/usr/bin/env python3
"""Post-hook: deliver the proactive answer to a suggested-reply tap.

Stdin: the model's answer. Printing it is delivery — heartbeat routes the
task's output to the user like any other user-facing task. The queue entry is
only dropped AFTER we have output to show; a model that came back empty
leaves the claim to be retaken (bounded by REPLY_FOLLOWUP_MAX_ATTEMPTS),
so a tap can never be silently swallowed — an unanswered tap is a dead end.

The task runs with untrusted-input (card bodies can quote external mail
text), so the model has no shell. Its one executable escape hatch is the
[ACTION:lark_auth_login] marker, which THIS deterministic hook runs — the
worst an injected body can trigger is a genuine auth link DM'd to the owner.

Settling also rewrites the pending-merge decision injection: the tap has been
acted on here, so Pascal's next real message must not trigger the action a
second time — but the conversation still gets told what happened.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import memorial
from core.safety import looks_like_error, strip_task_framing

_AUTH_MARKER = "[ACTION:lark_auth_login]"


def _settle(mid: str, answer: str) -> None:
    memorial.reply_followup_complete(mid)
    st = memorial.get_memorial(mid)
    title = str((st or {}).get("title", ""))
    label = str((st or {}).get("decided_label", ""))
    memorial.settle_decision_context(mid, (
        f"[奏折回复·已接手] 关于「{title}」Pascal 点了「{label}」，"
        f"后台已回应并处理（摘要：{answer[:200]}）。"
        "不要重复执行，只需在相关时引用这个结果。"))


def _run_auth_marker(text: str) -> str:
    """Execute the one whitelisted action marker, splice in its receipt."""
    if _AUTH_MARKER not in text:
        return text
    from core.actions import ActionProcessor
    ap = ActionProcessor(jarvis_dir=memorial.JARVIS_DIR,
                         memory_dir=memorial.JARVIS_DIR / "memory",
                         jobs_dir=memorial.JARVIS_DIR / "jobs")
    receipt = ap._do_lark_auth_login("")
    return (text.replace(_AUTH_MARKER, "").strip() + "\n" + receipt).strip()


def main() -> int:
    text = strip_task_framing(sys.stdin.read().strip())
    if not text or "HEARTBEAT_OK" in text or looks_like_error(text):
        return 0
    m = re.search(r"\[reply-followup (mem_[a-z0-9_]+)\]", text)
    text = _run_auth_marker(text if not m else text.replace(m.group(0), "").strip())
    if m:
        _settle(m.group(1), text)
    else:
        # No id echoed — still deliver. Settle the MOST RECENTLY claimed
        # request: this answer belongs to this cycle's claim. claimed[0]
        # (oldest by file order) could be a dead earlier claim awaiting
        # retake — settling that one would swallow its tap forever AND
        # leave this one queued for a duplicate answer.
        from core.jsonl import read_jsonl
        claimed = [r for r in read_jsonl(memorial._reply_followup_queue_path())
                   if int(r.get("taken_at") or 0)]
        if claimed:
            newest = max(claimed, key=lambda r: int(r.get("taken_at") or 0))
            _settle(str(newest.get("memorial_id")), text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
