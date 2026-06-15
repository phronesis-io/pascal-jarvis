"""Reply-based intent closure (REQ-64) — deterministic, no Feishu button backend.

In the entire live record, ALL closure events were via cli/ttl — ZERO via button
or reply. Pascal had no working way to close a loop: the card buttons depend on
the Feishu callback backend, and there was no reply parser at all. So health
items he asked for rot in 'awaiting'.

This module classifies Pascal's reply to a closure-question card into
done / recorded(=did-not-do, but noted) / na(=stop chasing), so bot.sh can call
record_closure(via='reply') DETERMINISTICALLY when the signal is clear, and fall
back to the main-session LLM only when it's genuinely ambiguous. Keyword-based,
fully testable, never raises.

Outcome semantics (matches core.intentions _CLOSURE_TERMINAL):
  done     — Pascal did it / it happened ("做了" "约了" "去了" "搞定")
  recorded — Pascal did NOT do it but it's noted ("没做" "没去" "改天" "下次")
  na       — stop chasing this ("不用了" "算了" "取消" "别追了")
  None     — ambiguous; defer to the LLM hint path (don't guess)
"""

from __future__ import annotations

import re

# Order matters: na (explicit stop) is checked before recorded/done so "不用追了"
# isn't misread as a negation→recorded. Each entry is (outcome, regex).
_RULES = [
    ("na", re.compile(r"不用追|别追|不用了|不用提醒|算了|取消(这个|吧|了)|别管|无所谓|不重要|删(了|掉)|关掉")),
    ("done", re.compile(r"做了|做完|搞定|约了|约上了|去了|去过|聊了|完成|搞定了|已经(做|约|去|聊)|弄好|处理好|解决了|✅|👍|搞掂")),
    ("recorded", re.compile(r"没做|没去|没约|没聊|还没|没有做|改天|下次|以后再|过几天|暂时不|先不|没时间|忘了|忘记|抽不出|没空")),
]

# A bare affirmation/negation when we KNOW it's a reply to a yes/no closure ask.
_BARE_YES = re.compile(r"^(是|对|嗯+|yes|y|好的?|👍|✅|做了|约了)[\s。.!！]*$", re.IGNORECASE)
_BARE_NO = re.compile(r"^(没|没有|不|no|n|还没|没做)[\s。.!！]*$", re.IGNORECASE)


def classify_reply(text: str) -> str | None:
    """Classify a closure reply → 'done'|'recorded'|'na'|None (ambiguous).

    Conservative: returns None unless a clear signal is present, so an
    ambiguous reply falls through to the LLM path rather than being
    mis-closed. Never raises.
    """
    if not text:
        return None
    t = text.strip()
    # Strip a leading quote-reply marker if present
    t = re.sub(r"^\[Replying to:.*?\]\s*", "", t, flags=re.DOTALL).strip()
    if not t:
        return None
    low = t[:200]
    # Bare yes/no (only meaningful as a reply to a yes/no ask)
    if _BARE_YES.match(low):
        return "done"
    if _BARE_NO.match(low):
        return "recorded"
    for outcome, rx in _RULES:
        if rx.search(low):
            return outcome
    return None


def short_result(text: str, limit: int = 80) -> str:
    """A one-line result string to store on the closure (record_closure result)."""
    t = re.sub(r"^\[Replying to:.*?\]\s*", "", (text or "").strip(), flags=re.DOTALL)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


if __name__ == "__main__":
    import sys
    txt = sys.argv[1] if len(sys.argv) > 1 else ""
    out = classify_reply(txt)
    # stdout contract for bot.sh: "<outcome>\t<result>" or empty line if None
    if out:
        print(f"{out}\t{short_result(txt)}")
    else:
        print("")
