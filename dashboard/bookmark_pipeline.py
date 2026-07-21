"""Bookmark processing pipeline: capture → analyze → store → resurface.

The 'analyze' step runs AI-powered summarization and tagging.
The 'resurface' step selects items for daily digest based on:
  - Recency (new items)
  - Spaced repetition (old items due for review)
  - Serendipity (random archive)
  - Context matching (current user focus)
"""

import json
import random
import time
from datetime import datetime, timedelta

from core.timeutil import now_local_str
from .db import (
    bookmark_add, bookmark_list, bookmark_search, bookmark_update, get_db
)

# Controlled vocabulary for auto-tags (prevents tag explosion)
TAG_VOCABULARY = [
    "ai", "agent", "llm", "ml", "research", "paper",
    "product", "startup", "vc", "finance", "market",
    "philosophy", "psychology", "neuroscience",
    "engineering", "architecture", "devtools",
    "health", "fitness", "nutrition",
    "culture", "film", "music", "books",
    "productivity", "habits", "learning",
    "eigenflux", "phronesis", "competition",
    "china", "global", "policy",
    "tutorial", "reference", "news", "opinion",
]


def capture(title: str, url: str = "", source: str = "manual",
            content: str = "") -> int:
    """Capture a new bookmark into the inbox."""
    return bookmark_add(
        title=title, url=url, source=source, content=content
    )


def get_resurface_candidates(count: int = 5) -> list[dict]:
    """Select items to resurface in daily digest. READ-ONLY (REQ-43).

    Mix: 2 new (inbox) + 2 spaced (old, due) + 1 random (serendipity)

    Selection no longer mutates surfaced_count/last_surfaced_at: the
    dashboard GET render calls this on every page view, so each refresh /
    monitoring probe silently advanced the spaced-repetition state (tonight's
    probes corrupted 6 bookmarks). Advancing the state is now an explicit
    act — call mark_surfaced() when the items were actually shown to Pascal.
    """
    db = get_db()
    candidates = []

    # 1. New items (inbox, never surfaced)
    new_items = db.execute(
        """SELECT * FROM bookmarks
           WHERE status = 'inbox' AND surfaced_count = 0
           ORDER BY created_at DESC LIMIT ?""",
        (max(2, count // 2),)
    ).fetchall()
    candidates.extend([dict(r) for r in new_items])

    # 2. Spaced repetition — items not surfaced in a while
    # Interval grows: 1d, 3d, 7d, 14d, 30d based on surfaced_count
    # last_surfaced_at is a local-naive string (now_local_str), while a bare
    # julianday('now') is UTC — that skew made every interval silently 8h
    # long. Compare both sides in local time.
    spaced_items = db.execute(
        """SELECT * FROM bookmarks
           WHERE status IN ('triaged', 'reading')
             AND last_surfaced_at IS NOT NULL
             AND julianday(datetime('now', 'localtime')) - julianday(last_surfaced_at) >
                 CASE surfaced_count
                   WHEN 1 THEN 1
                   WHEN 2 THEN 3
                   WHEN 3 THEN 7
                   WHEN 4 THEN 14
                   ELSE 30
                 END
           ORDER BY RANDOM() LIMIT ?""",
        (max(2, count // 3),)
    ).fetchall()
    candidates.extend([dict(r) for r in spaced_items])

    # 3. Serendipity — random from archive
    random_items = db.execute(
        """SELECT * FROM bookmarks
           WHERE status IN ('done', 'archived', 'triaged')
           ORDER BY RANDOM() LIMIT ?""",
        (max(1, count // 5),)
    ).fetchall()
    candidates.extend([dict(r) for r in random_items])

    # Deduplicate and limit
    seen_ids = set()
    result = []
    for item in candidates:
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            result.append(item)
    return result[:count]


def mark_surfaced(bookmark_ids: list[int]) -> None:
    """Advance the spaced-repetition state for items actually surfaced.

    The explicit user action that used to be a GET side effect: call this
    from a digest sender or a dashboard button — never from a render path.
    """
    db = get_db()
    now = now_local_str("%Y-%m-%dT%H:%M:%S")
    for bm_id in bookmark_ids:
        row = db.execute(
            "SELECT surfaced_count FROM bookmarks WHERE id = ?", (bm_id,)
        ).fetchone()
        if row is None:
            continue
        bookmark_update(
            bm_id,
            surfaced_count=(row[0] or 0) + 1,
            last_surfaced_at=now,
        )


def get_context_matches(query: str, limit: int = 3) -> list[dict]:
    """Find bookmarks related to the current conversation topic."""
    return bookmark_search(query, limit=limit)


def migrate_from_jsonl(jsonl_path: str) -> int:
    """Migrate existing watchlater.jsonl into SQLite."""
    from pathlib import Path
    p = Path(jsonl_path)
    if not p.exists():
        return 0
    count = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            bookmark_add(
                title=entry.get("title", "Untitled"),
                url=entry.get("url", ""),
                source=entry.get("source", "migrated"),
            )
            count += 1
        except (json.JSONDecodeError, Exception):
            continue
    return count
