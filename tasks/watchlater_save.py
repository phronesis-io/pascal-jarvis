#!/usr/bin/env python3
"""Save a content item to the watch-later list.

Usage:
    python3 watchlater_save.py <title> <url> [source]
    echo '{"title":"...","url":"..."}' | python3 watchlater_save.py

Appends to $MEMORY_DIR/system/watchlater.jsonl.
Deduplicates by URL. Caps at 50 entries.
Prints a confirmation message on stdout.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
STORE_FILE = MEMORY_DIR / "system" / "watchlater.jsonl"
MAX_ENTRIES = 50


def load_entries() -> list[dict]:
    entries = []
    if STORE_FILE.exists():
        for line in STORE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def save_entries(entries: list[dict]) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, STORE_FILE)


def _save_to_sqlite(title: str, url: str, source: str) -> bool:
    """Also save to the shared SQLite store (if available)."""
    try:
        from core.db import bookmark_add
        bookmark_add(title=title, url=url, source=source)
        return True
    except Exception:
        return False


def main() -> int:
    title = ""
    url = ""
    source = "natural"

    # Parse from args or stdin
    if len(sys.argv) >= 3:
        title = sys.argv[1]
        url = sys.argv[2]
        source = sys.argv[3] if len(sys.argv) >= 4 else "natural"
    else:
        raw = sys.stdin.read().strip()
        if raw:
            try:
                data = json.loads(raw)
                title = data.get("title", "")
                url = data.get("url", "")
                source = data.get("source", "natural")
            except json.JSONDecodeError:
                print("Error: invalid JSON input", file=sys.stderr)
                return 1

    if not url:
        print("Error: URL is required", file=sys.stderr)
        return 1

    entries = load_entries()

    # Deduplicate by URL
    existing_urls = {e.get("url", "") for e in entries}
    if url in existing_urls:
        print(f"已在收藏列表中: {title or url}")
        return 0

    # Append new entry
    entries.append({
        "ts": now_local_str("%Y-%m-%d %H:%M"),
        "title": title,
        "url": url,
        "status": "pending",
        "source": source,
    })

    # Cap at MAX_ENTRIES (keep newest)
    entries = entries[-MAX_ENTRIES:]

    save_entries(entries)

    # Dual-write to the shared SQLite store
    _save_to_sqlite(title, url, source)

    print(f"已收藏: {title or url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
