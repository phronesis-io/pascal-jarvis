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

# na = explicit "stop chasing".
_NA = re.compile(r"不用追|别追|不用了|不用提醒|算了|取消(这个|吧|了)?|别管|无所谓|不重要|删(了|掉)|关掉")
# A negation token NEAR a completion verb means "did NOT do it" (recorded), even
# though a done substring (做/约/去…) is present — red-team fix: "没做完",
# "做了一半还没弄完", "没搞定" were misread as done by a bare substring match.
_NEGATION = re.compile(r"没|还没|没有|未|尚未|没能|不曾|一半|没完|没弄完|没做完")
_DONE_VERB = re.compile(r"做|做完|搞定|约|去|聊|弄|完成|处理|解决|搞掂|读|写")
# Positive completion (no negation context).
_DONE = re.compile(r"做了|做完了?|搞定了?|约了|约上了|去了|去过|聊了|完成了?|弄好|处理好|解决了|✅|👍|搞掂|读完|写完")
# Explicit "did not do, but noted".
_RECORDED = re.compile(r"没做|没去|没约|没聊|没有做|改天|下次|以后再|过几天|暂时不|先不|没时间|忘了|忘记|抽不出|没空|还没")

# Bare affirmation/negation as a reply to a yes/no closure ask.
_BARE_YES = re.compile(r"^(是|对|yes|y|✅|做了|约了|去了)[\s。.!！]*$", re.IGNORECASE)
_BARE_NO = re.compile(r"^(没|没有|不|no|n|还没|没做|改天)[\s。.!！]*$", re.IGNORECASE)


def classify_reply(text: str) -> str | None:
    """Classify a closure reply → 'done'|'recorded'|'na'|None (ambiguous).

    Conservative + negation-aware: a negation near a completion verb is
    'recorded' (did NOT do it), never 'done'. Returns None unless a clear,
    unambiguous signal is present, so a borderline reply falls through to the
    LLM path rather than being mis-closed (closing is semi-destructive). Never
    raises.
    """
    if not text:
        return None
    t = re.sub(r"^\[Replying to:.*?\]\s*", "", text.strip(), flags=re.DOTALL).strip()
    if not t:
        return None
    low = t[:200]
    if _NA.search(low):
        return "na"
    # Bare ack/deny (only one token) — unambiguous
    if _BARE_NO.match(low):
        return "recorded"
    if _BARE_YES.match(low):
        return "done"
    has_neg = bool(_NEGATION.search(low))
    has_done = bool(_DONE.search(low))
    has_done_verb = bool(_DONE_VERB.search(low))
    has_recorded = bool(_RECORDED.search(low))
    # Negation near a completion verb → did NOT do it (recorded), even if a
    # done substring is present ("做了一半还没弄完", "没做完", "没搞定").
    if has_neg and has_done_verb:
        return "recorded"
    if has_recorded and not has_done:
        return "recorded"
    if has_done and not has_neg:
        return "done"
    # done token AND negation both present but no completion-verb adjacency →
    # genuinely ambiguous: defer to the LLM rather than guess.
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
