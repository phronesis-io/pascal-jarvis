#!/usr/bin/env python3
"""Post-hook: log each check-in with topic extraction + mechanical dedup.

Three-layer anti-repetition:
1. Pre-script injects ALL past topics as a blocklist (prompt-level)
2. This script extracts topic keywords and stores them in the log (data-level)
3. Before sending, checks topic overlap with history — blocks if too similar (gate-level)

Stdin: the check-in message Claude generated (markdown).
Stdout: same message if passes dedup, empty if blocked.
"""

import json
import os
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import companion
from core.card import build_card
from core.safety import (looks_like_error, parse_json_response,
                         strip_task_framing, is_idle_reply)
from core.jsonl import read_jsonl, write_jsonl
from core.lifelog import diet_append, split_diet_line
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
LOG_FILE = MEMORY_DIR / "system" / "checkin_log.jsonl"
MAX_ENTRIES = 40

# Overlap threshold: if this fraction of new topic tokens appear in any single
# past entry's topics, the checkin is considered a duplicate.
DEDUP_THRESHOLD = 0.6


def extract_topics(text: str) -> str:
    """Extract key names, concepts, and terms from a checkin message.

    Focuses on HIGH-SIGNAL identifiers: person names, book titles, core concepts.
    Avoids noisy long CJK phrases that dilute comparison.
    Returns a compact comma-separated string.
    """
    topics: list[str] = []

    # Book/paper titles in 《》
    for m in re.finditer(r"《(.+?)》", text):
        topics.append(m.group(1))

    # Quoted titles in English
    for m in re.finditer(r'["""](.+?)["""]', text):
        if len(m.group(1)) > 3:
            topics.append(m.group(1))

    # Names after ——  (Chinese citation pattern: ——人名，《书》)
    for m in re.finditer(r"——\s*(.+?)(?:[,，。\n]|$)", text):
        chunk = m.group(1).strip()
        name = re.split(r"[,，《]", chunk)[0].strip()
        if name and len(name) < 30:
            topics.append(name)

    # Capitalized multi-word names (English proper nouns)
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
        topics.append(m.group(1))

    # Single capitalized words that look like surnames/names (3+ letters)
    for m in re.finditer(r"\b([A-Z][a-z]{2,})\b", text):
        word = m.group(1)
        # Skip common English words
        if word.lower() not in {"the", "this", "that", "and", "for", "not", "but",
                                 "are", "was", "were", "has", "have", "had", "been",
                                 "will", "can", "may", "how", "what", "when", "where"}:
            topics.append(word)

    # CJK person names (2-4 chars, typically after known patterns)
    for m in re.finditer(r"(?:亚里士多德|苏格拉底|柏拉图|孔子|老子|庄子|孟子|海德格尔|尼采|康德|黑格尔|维特根斯坦|蒙田|笛卡尔|斯宾诺莎|叔本华|王德峰|王阳明|朱熹)", text):
        topics.append(m.group(0))

    # CJK compound terms — only 2-5 chars (targeted concepts, not full sentences)
    for m in re.finditer(r"(?<=[\s，。、：:])[\u4e00-\u9fff]{2,5}(?=[\s，。、：:])", text):
        topics.append(m.group(0))

    # Key domain markers are per-user: interests, projects, recurring themes.
    # The list lives in the gitignored data/checkin_topics_personal.txt (one
    # keyword per line); without it, topic extraction just relies on the
    # generic patterns above.
    personal_kw = Path(__file__).resolve().parent.parent / "data" / "checkin_topics_personal.txt"
    try:
        keywords = [w.strip() for w in
                    personal_kw.read_text(encoding="utf-8").splitlines() if w.strip()]
    except OSError:
        keywords = []
    for kw in keywords:
        if kw in text:
            topics.append(kw)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for t in topics:
        t_lower = t.lower().strip()
        if t_lower not in seen and t_lower and len(t_lower) >= 2:
            seen.add(t_lower)
            unique.append(t.strip())

    return ", ".join(unique[:12])


def split_topics(topics_str: str) -> list[str]:
    """Split topic string into individual topic entries."""
    if not topics_str:
        return []
    return [t.strip().lower() for t in re.split(r"[,，]+", topics_str) if t.strip()]


def is_duplicate(new_topics: str, entries: list[dict]) -> bool:
    """Check if new checkin's topics overlap too much with any past entry.

    Uses both exact token match AND substring containment to catch cases like
    '亚里士多德' appearing inside '亚里士多德有个词'.
    """
    new_items = split_topics(new_topics)
    if not new_items:
        return False

    for entry in entries:
        old_items = split_topics(entry.get("topics", ""))
        if not old_items:
            continue

        # Count matches: exact OR substring containment in either direction
        matches = 0
        for ni in new_items:
            for oi in old_items:
                if ni == oi or (len(ni) >= 3 and ni in oi) or (len(oi) >= 3 and oi in ni):
                    matches += 1
                    break

        min_size = min(len(new_items), len(old_items))
        if min_size > 0 and matches / min_size >= DEDUP_THRESHOLD:
            return True
    return False


def silent(reason: str) -> int:
    """Exit without a card, on the record.

    Every path out of this script that produces no card runs through here.
    Before 2026-08-02 they all just `return 0`, the heartbeat scored the run a
    success, and checkin went 10 days without saying a word while reporting
    `last_status: ok` across 708 runs. A decision to stay quiet is a decision;
    it has to leave a trace or nothing can ever notice it repeating.
    """
    try:
        companion.record_silence(reason)
    except Exception as exc:  # bookkeeping must never break the task
        print(f"[checkin] silence bookkeeping failed: {exc}", file=sys.stderr)
    return 0


def extract_kind(message: str) -> tuple[str, str]:
    """Pull the `KIND: <one of core.companion.KINDS>` line off the message.

    The kind is the unit of learning — without it every card lands in the
    ledger as an undifferentiated `source=checkin` and the four registers
    (followup / standing / notice / guide) cannot be told apart, which is why
    nothing could be learned from 23 cards' worth of taps.
    """
    pattern = re.compile(r"^\s*KIND[:：]\s*([A-Za-z_]+)\s*$", re.M)
    m = pattern.search(message)
    if not m:
        return message, companion.DEFAULT_KIND
    return pattern.sub("", message).strip(), companion.normalize_kind(m.group(1))


def main() -> int:
    message = sys.stdin.read().strip()
    # Suppress on the silence sentinel even if the model wrapped it with a header
    # line and/or trailing reasoning. A real check-in never contains this token,
    # so a substring check is safe — and far more robust than an exact match,
    # which leaked "🌿 关怀 / HEARTBEAT_OK + internal reasoning" cards to the user.
    if not message:
        return silent("empty model output")
    if is_idle_reply(message):
        return silent("model chose silence (HEARTBEAT_OK)")
    if looks_like_error(message):
        print("[checkin] skipping — looks like error output", file=sys.stderr)
        return silent("output looked like an error")

    # Unwrap a JSON envelope. The checkin prompt asks for plain markdown, but
    # the model sometimes reuses the intention-check response shape
    # ({"response": "...", "action": "notify"}) from elsewhere in the
    # heartbeat context — and the raw envelope went onto Pascal's card
    # verbatim (his 2026-07-14 complaint: the card was unreadable JSON).
    parsed = parse_json_response(message)
    if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
        action = str(parsed.get("action", "notify")).lower()
        if action in ("silent", "skip", "none"):
            print(f"[checkin] model chose action={action} — no card", file=sys.stderr)
            return silent(f"model chose action={action}")
        message = parsed["response"].strip()
        if not message:
            return silent("JSON envelope carried an empty response")

    # Echoed prompt framing ("[CHECKIN]", "=== TASK: checkin ===",
    # "[2026-07-19 09:16] checkin") reached cards verbatim through 7/20.
    message = strip_task_framing(message)
    if not message:
        return silent("nothing left after stripping prompt framing")

    # The model must not author this card's buttons (8/3: an imitated
    # 「OPTIONS: 说说这个|知道了」line displaced the companion preset and cost
    # the card its「这类不必」button). The REAL enforcement is central —
    # memorial.PRESET_LOCKED_SOURCES at create(), covering every entry path —
    # this local strip is belt-and-braces for the stdout body, and reuses
    # memorial's own pattern so the producer of the bug and the guard against
    # it cannot drift apart.
    from core.memorial import _OPTIONS_LINE_RE
    message = "\n".join(
        line for line in message.splitlines()
        if not _OPTIONS_LINE_RE.match(line)).strip()
    if not message:
        return silent("nothing left after stripping an OPTIONS line")

    # KIND must come off before THEMES/DIET: like them it is a trailing
    # contract line, and leaving it in would put "KIND: notice" on the card.
    message, kind = extract_kind(message)
    if not message:
        return silent("nothing left after stripping KIND")

    # THEMES contract (7/21 乱联系根修 RC2): the prompt asks the model to end
    # with "THEMES: 概念1, 概念2" — 2-4 meaning-level tags used for dedup by
    # meaning. Stripped FIRST: it is the last line, so stripping DIET first
    # would miss a "…\nDIET:…\nTHEMES:…" tail and leak the DIET line onto
    # the card (red-team 7/21 finding 3).
    themes = ""
    m = re.search(r"\n\s*THEMES[:：]\s*(.+?)\s*$", message)
    if m:
        themes = m.group(1).strip()
        message = message[:m.start()].rstrip()
        if not message:
            return silent("nothing left after stripping THEMES")

    # REQ-114 diet capture: the checkin prompt may end with an OPTIONAL
    # structured line — "DIET: 午|牛肉面、青菜" — emitted ONLY when the user
    # explicitly said what they ate (contract in checkin_pre.sh). It is
    # stripped from the card (never user-facing) and appended to the
    # gitignored data/diet_log.jsonl. Deliberately no keyword fallback here:
    # this message is assistant-authored, so regexing its prose for food
    # would log hallucinated meals. Logging must never break a checkin.
    message, diet_entry = split_diet_line(message)
    if diet_entry:
        try:
            diet_entry["source"] = "checkin"
            diet_append(diet_entry)
        except Exception as e:
            print(f"[checkin] diet log failed: {e}", file=sys.stderr)
    if not message:
        return silent("nothing left after stripping DIET")

    # Read existing entries
    entries = read_jsonl(LOG_FILE)

    # Meaning-level tags when the model provided them; regex fallback otherwise
    topics = themes or extract_topics(message)

    # Mechanical dedup gate
    if is_duplicate(topics, entries):
        print(f"[checkin] BLOCKED duplicate — topics: {topics}", file=sys.stderr)
        return silent(f"blocked as duplicate of a recent theme: {topics}")

    # Append new entry with topics
    entries.append({
        "ts": now_local_str("%Y-%m-%d %H:%M"),
        "content": message,
        "topics": topics,
    })
    entries = entries[-MAX_ENTRIES:]
    write_jsonl(LOG_FILE, entries)

    # Determine checkin mode: wellbeing or connection
    wellbeing_keywords = ("身体", "感受", "状态", "休息", "睡", "累", "疲", "健康", "压力")
    has_question = "?" in message or "？" in message
    is_wellbeing = has_question or any(kw in message for kw in wellbeing_keywords)
    header = "🌿 关怀" if is_wellbeing else "💡 联系"

    # Output as Lark card (single line). The KIND rides along in the context
    # marker so the ledger can score this card against its own register later.
    card = build_card(
        header, message, source="checkin",
        context=json.dumps({"kind": kind}, ensure_ascii=False),
        work_receipt="读取近期对话与关怀记录、完成主题去重和时机判断",
    )
    if not card:
        return silent("card builder suppressed the output")
    print(card)
    try:
        companion.record_spoke(kind, topics)
    except Exception as e:
        print(f"[checkin] spoke bookkeeping failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
