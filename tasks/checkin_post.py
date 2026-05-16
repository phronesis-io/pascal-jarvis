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
from core.card import build_card
from core.safety import looks_like_error
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

    # Key domain markers
    for kw in ["NBA", "骑士", "G7", "EigenFlux", "A2A", "Agent", "黄金", "基金",
               "声乐", "围棋", "死活题", "哲学", "主题A", "主题B"]:
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


def main() -> int:
    message = sys.stdin.read().strip()
    if not message or message == "HEARTBEAT_OK":
        return 0
    if looks_like_error(message):
        print("[checkin] skipping — looks like error output", file=sys.stderr)
        return 0

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Read existing entries
    entries: list[dict] = []
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Extract topics from new message
    topics = extract_topics(message)

    # Mechanical dedup gate
    if is_duplicate(topics, entries):
        print(f"[checkin] BLOCKED duplicate — topics: {topics}", file=sys.stderr)
        return 0  # empty stdout → no message sent to user

    # Append new entry with topics
    entries.append({
        "ts": now_local_str("%Y-%m-%d %H:%M"),
        "content": message,
        "topics": topics,
    })
    entries = entries[-MAX_ENTRIES:]

    # Atomic write
    tmp = LOG_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, LOG_FILE)

    # Determine checkin mode: wellbeing or connection
    wellbeing_keywords = ("身体", "感受", "状态", "休息", "睡", "累", "疲", "健康", "压力")
    has_question = "?" in message or "？" in message
    is_wellbeing = has_question or any(kw in message for kw in wellbeing_keywords)
    header = "🌿 关怀" if is_wellbeing else "💡 联系"

    # Output as Lark card (single line)
    print(build_card(header, message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
