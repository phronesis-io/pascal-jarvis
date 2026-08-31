"""Mechanical one-card-one-matter splitting (一张卡一件事, REQ-117).

the owner's 7/10 decree: a memorial card states ONE matter. The 7/15 fix
(458ce63) was a prompt-level contract for LLM-authored digests — it cannot
reach mechanically composed bodies, and on 7/21 the calendar 日程变动 card
merged three 改期 lines into one card again. This module is the code-level
backstop applied where card bodies are born (calendar_sync_post) and where
they are converted to memorials (core.memorial).

Conservative by design — false splits are worse than occasional misses, so a
body is only split when its structure is unambiguously a list of independent
matters:

1. Change-list bodies: 2–3 lines matching the 改期/新增/取消 pattern and
   NOTHING else (except the …另有 N 项 overflow counter, which rides on the
   last matter). A change line embedded in analytical prose never splits, and
   a list longer than MERGE_CHANGE_LIST_ABOVE stays merged — past that length
   it is a batch of calendar churn, not N matters worth N buzzes.
2. Bold-section bodies: the body starts with a standalone ``**heading**``
   line and contains ≥2 such headings, none of which is a generic
   sub-section word (背景/建议/下一步…) — those mark ONE matter's internal
   structure, not multiple matters.

Pure regex/structure, no LLM call. Callers log when len(result) > 1 so
splits stay auditable.
"""

from __future__ import annotations

import re

# One schedule/state change per line: "改期：X — a → b", "新增：… 会议",
# optionally bulleted. The verb set is deliberately small and unambiguous.
_MATTER_LINE_RE = re.compile(
    r"^\s*(?:[-•*·]\s*)?(?:改期|新增|取消|变更|延期)\s*[:：]")

# calendar_sync_post's overflow counter — not a matter of its own.
_OVERFLOW_LINE_RE = re.compile(r"^\s*[….⋯]*\s*另有\s*\d+\s*项")

# A standalone bold heading line, e.g. "**知会 · FlashRT**". The group
# excludes '*' so inline bold inside a prose line ("**A** vs **B**") never
# reads as a heading.
_BOLD_HEADING_RE = re.compile(r"^\s*\*\*([^*\n]{1,80})\*\*\s*$")

# Headings that structure ONE matter internally. If any heading in a body is
# one of these, the bold sections are exposition, not separate matters.
_GENERIC_SECTION_WORDS = {
    "背景", "上下文", "现状", "数据", "细节", "说明", "原因", "分析",
    "影响", "风险", "建议", "方案", "对策", "下一步", "结论", "总结",
    "要点", "备注", "进展", "状态", "问题", "证据",
}

# Splitting must never turn one merged card into a phone-buzz storm: beyond
# this many matters the remainder is kept together in the final card.
MAX_SPLIT_CARDS = 6

# A change list stops being "one matter each" once it is long enough to read
# as a batch. Owner feedback established both sides: three shifted meetings
# should be separate cards, while a long change list should be merged. Both
# hold at once — split a short list, merge a long
# one, because a long one IS one matter ("your calendar moved a lot today").
MERGE_CHANGE_LIST_ABOVE = 3


def _cap(chunks: list[str]) -> list[str]:
    if len(chunks) <= MAX_SPLIT_CARDS:
        return chunks
    head = chunks[:MAX_SPLIT_CARDS - 1]
    return head + ["\n".join(chunks[MAX_SPLIT_CARDS - 1:])]


def _split_change_lines(body: str) -> list[str] | None:
    """Rule 1: a body that IS a list of 改期/新增/取消 lines."""
    matters: list[str] = []
    overflow: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _MATTER_LINE_RE.match(stripped):
            matters.append(stripped)
        elif _OVERFLOW_LINE_RE.match(stripped):
            overflow.append(stripped)
        else:
            # Any other prose means this is a composed narrative — leave it.
            return None
    if len(matters) < 2:
        return None
    if len(matters) > MERGE_CHANGE_LIST_ABOVE:
        # A batch, not N independent matters — one card carrying every line.
        return None
    if overflow:
        matters[-1] = matters[-1] + "\n" + "\n".join(overflow)
    return matters


def _heading_word(heading: str) -> str:
    """Normalize a heading for the generic-word check: drop emoji, digits,
    punctuation and whitespace so "🔔 建议：" still reads as 建议."""
    return re.sub(r"[^一-鿿A-Za-z]", "", heading)


def _split_bold_sections(body: str) -> list[str] | None:
    """Rule 2: a body that is ≥2 self-contained ``**heading**`` sections."""
    lines = body.splitlines()
    heading_idx = [i for i, ln in enumerate(lines) if _BOLD_HEADING_RE.match(ln)]
    if len(heading_idx) < 2:
        return None
    # A preamble before the first heading means the author composed ONE
    # narrative with sub-sections — never split that.
    if any(ln.strip() for ln in lines[:heading_idx[0]]):
        return None
    for i in heading_idx:
        word = _heading_word(_BOLD_HEADING_RE.match(lines[i]).group(1))
        if not word or word in _GENERIC_SECTION_WORDS:
            return None
    bounds = heading_idx + [len(lines)]
    chunks = []
    for start, end in zip(bounds, bounds[1:]):
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            chunks.append(chunk)
    # Headings without any body text look like an outline, not matters.
    if any(len(c.splitlines()) < 2 for c in chunks):
        return None
    return chunks if len(chunks) >= 2 else None


def split_matters(body: str) -> list[str]:
    """Split a card body into one-matter chunks.

    Returns ``[body]`` unchanged (the common case) unless the body is
    unambiguously multiple matters per the module rules. Never returns an
    empty list for non-empty input.
    """
    text = str(body or "").strip()
    if not text:
        return [body]
    chunks = _split_change_lines(text) or _split_bold_sections(text)
    if not chunks:
        return [body]
    return _cap(chunks)
