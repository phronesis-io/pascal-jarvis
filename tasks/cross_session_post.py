#!/usr/bin/env python3
"""Post-hook: write cross-session digest to memory.

Receives Claude's summary from stdin and writes to memory/system/cross_session_digest.md.
Keeps max 50 lines, newest first. No user-facing output (silent task).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error, parse_json_response
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
DIGEST_FILE = MEMORY_DIR / "system" / "cross_session_digest.md"
MAX_LINES = 50

HEADER = """\
---
name: Cross-Session Digest
description: Recent activity from other Claude Code projects
type: reference
---

# Cross-Session Digest
"""


def _normalize(text: str) -> str:
    """Normalize text for dedup comparison: lowercase, collapse whitespace, strip timestamps."""
    import re
    text = text.lower()
    text = re.sub(r"\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}", "", text)  # strip timestamps
    text = re.sub(r"[（）()\"'\s]+", " ", text).strip()
    return text


def _char_ngrams(text: str, n: int = 3) -> set:
    """Extract character n-grams for language-agnostic comparison."""
    return {text[i:i+n] for i in range(len(text) - n + 1)} if len(text) >= n else {text}


def _is_duplicate(new_raw: str, existing_body: str) -> bool:
    """Check if new entry is essentially the same as the most recent entry.
    Uses character trigram Jaccard similarity — works for Chinese+English mixed text.
    Short texts require higher similarity to avoid false positives.
    """
    if not existing_body:
        return False
    import re
    # Extract the first entry's content (up to the next ## header)
    first_entry_match = re.match(r"\n## [^\n]+\n(.*?)(?=\n## |\Z)", existing_body, re.DOTALL)
    if not first_entry_match:
        return False
    last_content = _normalize(first_entry_match.group(1))
    new_content = _normalize(new_raw)
    if not last_content or not new_content:
        return False
    # Skip dedup for very short texts (< 15 chars) — too little signal
    if len(new_content) < 15 or len(last_content) < 15:
        return new_content == last_content  # exact match only for short texts
    # Character trigram Jaccard similarity
    last_ngrams = _char_ngrams(last_content)
    new_ngrams = _char_ngrams(new_content)
    if not new_ngrams or not last_ngrams:
        return False
    intersection = len(last_ngrams & new_ngrams)
    union = len(last_ngrams | new_ngrams)
    similarity = intersection / union if union else 0
    # Use high threshold — only skip near-exact duplicates.
    # Previous threshold (0.45) was too aggressive: ongoing work on the same
    # project produces similar-looking digests that are actually different updates.
    threshold = 0.75
    return similarity > threshold


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[cross-session] skipping — output looks like error", file=sys.stderr)
        return 0

    # Parse JSON envelope: {"digest": "...", "user_message": "..."}
    # Not JSON → envelope is None, treat raw as a plain digest (backward compatible).
    user_message = ""
    envelope = parse_json_response(raw)
    if envelope is not None and "digest" in envelope:
        user_message = envelope.get("user_message", "").strip()
        raw = envelope["digest"].strip()
        if not raw:
            return 0

    # If there's a user-facing message, print to stdout (sent to Lark)
    if user_message:
        print(f"📡 跨 Session 动态：{user_message}")

    # Skip consecutive "No new data" entries — they waste index space
    is_no_data = "no new data" in raw.lower() or "no significant" in raw.lower()
    if is_no_data and DIGEST_FILE.exists():
        content = DIGEST_FILE.read_text(encoding="utf-8")
        # Check if the most recent entry is also "no new data"
        import re as _re
        first_entry = _re.search(r"\n## [^\n]+\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        if first_entry and "no new data" in first_entry.group(1).lower():
            print("[cross-session] skipping — consecutive 'No new data' entry", file=sys.stderr)
            return 0

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "system").mkdir(parents=True, exist_ok=True)

    # Read existing entries (skip header)
    existing_body = ""
    if DIGEST_FILE.exists():
        content = DIGEST_FILE.read_text(encoding="utf-8")
        # Split off the header (everything before the first ## date entry)
        parts = content.split("\n## ", 1)
        if len(parts) > 1:
            existing_body = "\n## " + parts[1]

    # Dedup: skip if new content is essentially the same as last entry
    if _is_duplicate(raw, existing_body):
        print("[cross-session] skipping — duplicate of latest entry", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d %H:%M")
    new_entry = f"\n## {ts}\n{raw.strip()}\n"

    # Combine: new entry first, then existing
    combined = new_entry + existing_body

    # Limit to MAX_LINES of body content
    body_lines = combined.strip().splitlines()
    if len(body_lines) > MAX_LINES:
        body_lines = body_lines[:MAX_LINES]

    final = HEADER + "\n".join(body_lines) + "\n"

    # Atomic write
    tmp = DIGEST_FILE.with_suffix(".md.tmp")
    tmp.write_text(final, encoding="utf-8")
    os.replace(tmp, DIGEST_FILE)

    print(f"[cross-session] digest updated at {ts}", file=sys.stderr)
    # No stdout output — silent task
    return 0


if __name__ == "__main__":
    sys.exit(main())
