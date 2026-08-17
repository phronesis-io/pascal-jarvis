"""Read-only operational projections from Jarvis's shared SQLite state."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


def _db_path(root: str | Path) -> Path:
    return Path(root) / "data" / "jarvis.db"


def _open(root: str | Path) -> sqlite3.Connection | None:
    path = _db_path(root)
    if not path.is_file():
        return None
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=2000")
        return db
    except sqlite3.Error:
        return None


def delivery_overview(
    root: str | Path,
    *,
    now: float | None = None,
    queue_overdue_grace_seconds: float = 15 * 60,
) -> dict | None:
    """Return queue/dead-letter counts, or None before the migration exists."""
    now = time.time() if now is None else float(now)
    overdue_before = now - max(0.0, float(queue_overdue_grace_seconds))
    db = _open(root)
    if db is None:
        return None
    try:
        states = {
            str(row["state"]): int(row["count"])
            for row in db.execute(
                "SELECT state,COUNT(*) AS count FROM delivery_envelopes "
                "GROUP BY state")
        }
        dead_letters = int(db.execute(
            "SELECT COUNT(*) FROM delivery_dead_letters "
            "WHERE notified_epoch IS NULL").fetchone()[0])
        queued_rows = [
            dict(row) for row in db.execute(
                "SELECT id,source,kind,route_channel,last_error,created_epoch,"
                "next_attempt_epoch,attempts FROM delivery_envelopes "
                "WHERE state='queued' ORDER BY created_epoch LIMIT 20")
        ]
        queued_overdue = int(db.execute(
            "SELECT COUNT(*) FROM delivery_envelopes WHERE state='queued' "
            "AND ((next_attempt_epoch IS NULL AND created_epoch<=?) "
            "OR next_attempt_epoch<=?)",
            (overdue_before, overdue_before),
        ).fetchone()[0])
        next_queued_epoch = db.execute(
            "SELECT MIN(next_attempt_epoch) FROM delivery_envelopes "
            "WHERE state='queued' AND next_attempt_epoch>?",
            (now,),
        ).fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return {
        "queued": states.get("queued", 0),
        "attempting": states.get("attempting", 0),
        "delivered": states.get("delivered", 0),
        "read": states.get("read", 0),
        "acted": states.get("acted", 0),
        "suppressed": states.get("suppressed", 0),
        "failed": states.get("failed", 0),
        "dead_letters": dead_letters,
        # Compatibility key for the existing ops metric.
        "consec_fails": dead_letters,
        "queued_items": queued_rows,
        "queued_overdue": queued_overdue,
        "next_queued_epoch": next_queued_epoch,
        "source": "sqlite",
    }


def breach_overview(root: str | Path, limit: int = 100) -> list[dict] | None:
    """Return active intent breaches, or None before the migration exists."""
    db = _open(root)
    if db is None:
        return None
    try:
        rows = db.execute(
            "SELECT id,payload,notify_attempts,created_epoch "
            "FROM intent_breaches WHERE retired_epoch IS NULL "
            "ORDER BY created_epoch LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    result = []
    for row in rows:
        try:
            item = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            item = {}
        if not isinstance(item, dict):
            item = {}
        item.setdefault("id", str(row["id"]))
        item["notify_attempts"] = int(row["notify_attempts"] or 0)
        item["created_epoch"] = float(row["created_epoch"] or 0)
        result.append(item)
    return result
