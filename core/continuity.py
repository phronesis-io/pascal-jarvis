"""Durable cross-device handoffs for existing Jarvis work objects.

A handoff moves the next interaction between desktop and phone. It never
copies the Memorial, Matter, or Delegation it points at; those stores remain
authoritative.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

SURFACES = {"desktop", "mobile"}
ENTITY_TYPES = {"memorial", "matter", "delegation"}
ACTIVE_STATES = {"open", "claimed"}
TERMINAL_STATES = {"completed", "cancelled"}


_HANDOFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS surface_handoffs (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    matter_id TEXT NOT NULL DEFAULT '',
    from_surface TEXT NOT NULL,
    to_surface TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'local',
    created_epoch REAL NOT NULL,
    claimed_epoch REAL,
    completed_epoch REAL,
    delivery_id TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_surface_handoff_active_unique
    ON surface_handoffs(entity_type, entity_id, to_surface)
    WHERE status IN ('open', 'claimed');
CREATE INDEX IF NOT EXISTS idx_surface_handoff_target
    ON surface_handoffs(to_surface, status, created_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_surface_handoff_entity
    ON surface_handoffs(entity_type, entity_id, created_epoch DESC);
"""
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: dict[Path, tuple[int, int]] = {}


def _database_path() -> Path:
    from dashboard.db import _db_path
    return Path(_db_path())


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        resolved = path.resolve()
        with _SCHEMA_LOCK:
            stat = resolved.stat()
            identity = (int(stat.st_dev), int(stat.st_ino))
            if _SCHEMA_READY.get(resolved) != identity:
                db.executescript(_HANDOFF_SCHEMA)
                db.commit()
                stat = resolved.stat()
                _SCHEMA_READY[resolved] = (
                    int(stat.st_dev), int(stat.st_ino))
        return db
    except Exception:
        db.close()
        raise


def _decode(row) -> dict:
    item = dict(row)
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        item["metadata"] = {}
    return item


def _validate(entity_type: str, entity_id: str,
              from_surface: str, to_surface: str) -> None:
    if entity_type not in ENTITY_TYPES:
        raise ValueError("entity_type must be memorial, matter, or delegation")
    if not str(entity_id or "").strip():
        raise ValueError("entity_id is required")
    if from_surface not in SURFACES or to_surface not in SURFACES:
        raise ValueError("surface must be desktop or mobile")
    if from_surface == to_surface:
        raise ValueError("handoff must cross surfaces")


def _require_entity(entity_type: str, entity_id: str) -> None:
    if entity_type == "memorial":
        from core.memorial import get_memorial
        exists = get_memorial(entity_id) is not None
    elif entity_type == "matter":
        from core.matters import get_matter
        exists = get_matter(
            entity_id, include_links=False, include_events=False) is not None
    else:
        from core.delegations import DelegationNotFound, DelegationStore
        try:
            DelegationStore().get(entity_id)
            exists = True
        except DelegationNotFound:
            exists = False
    if not exists:
        raise KeyError(entity_id)


def _notify_mobile(handoff: dict) -> dict:
    from core.delivery import DeliveryEnvelope, DeliveryPipeline

    entity_id = str(handoff["entity_id"])
    title = str(handoff.get("title") or "有一项工作等你接着处理")
    root = Path(
        os.environ.get("JARVIS_DIR")
        or Path(__file__).resolve().parent.parent
    )
    pipeline = DeliveryPipeline(root, db_path=_database_path())
    is_matter = handoff["entity_type"] == "matter"
    is_delegation = handoff["entity_type"] == "delegation"
    matter_id = str(handoff.get("matter_id") or (
        entity_id if is_matter else ""))
    result = pipeline.deliver(DeliveryEnvelope(
        source="surface-handoff",
        kind="push",
        payload={
            "title": "Jarvis · 发到手机",
            "text": title,
            "url": (
                f"/matters/{entity_id}"
                if is_matter
                else (
                    f"/delegations/{entity_id}"
                    if is_delegation
                    else f"/items/{entity_id}"
                )
            ),
            "matter_id": matter_id,
        },
        attention="reply",
        requested_channel="push",
        urgent=True,
        conversation_bound=True,
        memorial_id=entity_id if handoff["entity_type"] == "memorial" else "",
        matter_id=matter_id,
        dedup_key=f"surface-handoff:{handoff['id']}",
        metadata={
            "bypass_quiet": True,
            "bypass_throttle": True,
            "paired_only": True,
            "suppress_dead_letter": True,
            "from_surface": handoff["from_surface"],
            "to_surface": handoff["to_surface"],
        },
    ))
    return {
        "delivery_id": result.delivery_id,
        "delivery_state": result.state,
        "delivery_reason": result.reason,
    }


def create_handoff(
    entity_type: str,
    entity_id: str,
    *,
    from_surface: str,
    to_surface: str,
    title: str = "",
    matter_id: str = "",
    note: str = "",
    created_by: str = "local",
    metadata: dict | None = None,
    notify: bool = True,
    clock=time.time,
) -> dict:
    """Create or reuse one active handoff for an entity and target surface."""
    entity_type = str(entity_type or "").strip()
    entity_id = str(entity_id or "").strip()
    from_surface = str(from_surface or "").strip()
    to_surface = str(to_surface or "").strip()
    _validate(entity_type, entity_id, from_surface, to_surface)
    _require_entity(entity_type, entity_id)
    now = float(clock())
    handoff_id = f"hop_{uuid.uuid4().hex}"
    with closing(_connect()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM surface_handoffs "
                "WHERE entity_type=? AND entity_id=? AND to_surface=? "
                "AND status IN ('open','claimed') "
                "ORDER BY created_epoch DESC LIMIT 1",
                (entity_type, entity_id, to_surface),
            ).fetchone()
            if existing:
                db.commit()
                return {**_decode(existing), "created": False}
            db.execute(
                """
                INSERT INTO surface_handoffs (
                    id,entity_type,entity_id,matter_id,from_surface,to_surface,
                    status,title,note,created_by,created_epoch,metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    handoff_id, entity_type, entity_id, str(matter_id or ""),
                    from_surface, to_surface, "open", str(title or "")[:300],
                    str(note or "")[:1000], str(created_by or "local")[:120],
                    now, json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            row = db.execute(
                "SELECT * FROM surface_handoffs WHERE id=?", (handoff_id,)
            ).fetchone()
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            existing = db.execute(
                "SELECT * FROM surface_handoffs "
                "WHERE entity_type=? AND entity_id=? AND to_surface=? "
                "AND status IN ('open','claimed') "
                "ORDER BY created_epoch DESC LIMIT 1",
                (entity_type, entity_id, to_surface),
            ).fetchone()
            if existing:
                return {**_decode(existing), "created": False}
            raise
        except Exception:
            db.rollback()
            raise

    item = {**_decode(row), "created": True}
    if to_surface == "mobile" and notify:
        try:
            delivery = _notify_mobile(item)
        except Exception as exc:
            delivery = {
                "delivery_id": "",
                "delivery_state": "failed",
                "delivery_reason": str(exc)[:300],
            }
        if delivery["delivery_id"]:
            with closing(_connect()) as db, db:
                db.execute(
                    "UPDATE surface_handoffs SET delivery_id=? WHERE id=?",
                    (delivery["delivery_id"], handoff_id),
                )
        item.update(delivery)
    return item


def list_handoffs(
    *,
    target_surface: str = "",
    status: str = "active",
    limit: int = 100,
) -> list[dict]:
    conditions, params = [], []
    if target_surface:
        if target_surface not in SURFACES:
            raise ValueError("target_surface must be desktop or mobile")
        conditions.append("to_surface=?")
        params.append(target_surface)
    if status == "active":
        conditions.append("status IN ('open','claimed')")
    elif status:
        if status not in ACTIVE_STATES | TERMINAL_STATES:
            raise ValueError("invalid handoff status")
        conditions.append("status=?")
        params.append(status)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(max(1, min(int(limit), 500)))
    with closing(_connect()) as db:
        rows = db.execute(
            f"SELECT * FROM surface_handoffs{where} "
            "ORDER BY created_epoch DESC LIMIT ?",
            params,
        ).fetchall()
    return [_decode(row) for row in rows]


def get_handoff(handoff_id: str) -> dict | None:
    with closing(_connect()) as db:
        existing = db.execute(
            "SELECT * FROM surface_handoffs WHERE id=?", (str(handoff_id),)
        ).fetchone()
    return _decode(existing) if existing else None


def claim_handoff(
    handoff_id: str,
    *,
    surface: str,
    clock=time.time,
) -> dict:
    if surface not in SURFACES:
        raise ValueError("surface must be desktop or mobile")
    with closing(_connect()) as db, db:
        row = db.execute(
            "SELECT * FROM surface_handoffs WHERE id=?", (str(handoff_id),)
        ).fetchone()
        if not row:
            raise KeyError(handoff_id)
        if str(row["to_surface"]) != surface:
            raise ValueError("handoff belongs to another surface")
        if str(row["status"]) == "open":
            db.execute(
                "UPDATE surface_handoffs SET status='claimed',claimed_epoch=? "
                "WHERE id=? AND status='open'",
                (float(clock()), str(handoff_id)),
            )
        updated = db.execute(
            "SELECT * FROM surface_handoffs WHERE id=?", (str(handoff_id),)
        ).fetchone()
    return _decode(updated)


def complete_handoff(
    handoff_id: str,
    *,
    completion_source: str = "",
    clock=time.time,
) -> dict:
    with closing(_connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM surface_handoffs WHERE id=?", (str(handoff_id),)
        ).fetchone()
        if not row:
            raise KeyError(handoff_id)
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        if str(row["status"]) in ACTIVE_STATES:
            if completion_source:
                metadata["completion_source"] = str(completion_source)
                metadata["completion_previous_status"] = str(row["status"])
            db.execute(
                "UPDATE surface_handoffs "
                "SET status='completed',completed_epoch=?,metadata=? "
                "WHERE id=? AND status IN ('open','claimed')",
                (
                    float(clock()),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    str(handoff_id),
                ),
            )
        elif not completion_source and metadata.get("completion_source"):
            metadata.pop("completion_source", None)
            metadata.pop("completion_previous_status", None)
            metadata["completion_confirmed_by"] = "manual"
            db.execute(
                "UPDATE surface_handoffs SET metadata=? WHERE id=?",
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    str(handoff_id),
                ),
            )
        updated = db.execute(
            "SELECT * FROM surface_handoffs WHERE id=?", (str(handoff_id),)
        ).fetchone()
    return _decode(updated)


def complete_entity_handoffs(
    entity_type: str,
    entity_id: str,
    *,
    completion_source: str = "",
    clock=time.time,
) -> int:
    if entity_type not in ENTITY_TYPES:
        raise ValueError("entity_type must be memorial, matter, or delegation")
    with closing(_connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = float(clock())
        if not completion_source:
            changed = db.execute(
                "UPDATE surface_handoffs "
                "SET status='completed',completed_epoch=? "
                "WHERE entity_type=? AND entity_id=? "
                "AND status IN ('open','claimed')",
                (now, entity_type, str(entity_id)),
            ).rowcount
            return int(changed)
        rows = db.execute(
            "SELECT id,status,metadata FROM surface_handoffs "
            "WHERE entity_type=? AND entity_id=? "
            "AND status IN ('open','claimed')",
            (entity_type, str(entity_id)),
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["completion_source"] = str(completion_source)
            metadata["completion_previous_status"] = str(row["status"])
            db.execute(
                "UPDATE surface_handoffs "
                "SET status='completed',completed_epoch=?,metadata=? "
                "WHERE id=? AND status IN ('open','claimed')",
                (
                    now,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    str(row["id"]),
                ),
            )
    return len(rows)


def reopen_entity_handoffs(
    entity_type: str,
    entity_id: str,
    *,
    completion_source: str,
    legacy_completed_at: float | None = None,
    legacy_window_seconds: float = 60.0,
) -> int:
    """Undo only handoff completions owned by one terminal projection."""
    if entity_type not in ENTITY_TYPES:
        raise ValueError("entity_type must be memorial, matter, or delegation")
    if not str(completion_source or "").strip():
        raise ValueError("completion_source is required")
    changed = 0
    with closing(_connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        active_targets = {
            str(row["to_surface"])
            for row in db.execute(
                "SELECT to_surface FROM surface_handoffs "
                "WHERE entity_type=? AND entity_id=? "
                "AND status IN ('open','claimed')",
                (entity_type, str(entity_id)),
            ).fetchall()
        }
        rows = db.execute(
            "SELECT * FROM surface_handoffs "
            "WHERE entity_type=? AND entity_id=? AND status='completed' "
            "ORDER BY created_epoch DESC,id DESC",
            (entity_type, str(entity_id)),
        ).fetchall()
        seen_targets = set(active_targets)
        for row in rows:
            target_surface = str(row["to_surface"])
            if target_surface in seen_targets:
                continue
            seen_targets.add(target_surface)
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            owned = metadata.get("completion_source") == completion_source
            legacy_owned = False
            if (
                not metadata.get("completion_source")
                and legacy_completed_at is not None
            ):
                completed_epoch = float(row["completed_epoch"] or 0)
                created_epoch = float(row["created_epoch"] or 0)
                terminal_epoch = float(legacy_completed_at)
                legacy_owned = bool(
                    created_epoch <= terminal_epoch
                    and terminal_epoch <= completed_epoch
                    <= terminal_epoch + max(1.0, legacy_window_seconds)
                )
            if not (owned or legacy_owned):
                continue
            previous = str(
                metadata.get("completion_previous_status") or ""
            )
            if previous not in ACTIVE_STATES:
                previous = (
                    "claimed"
                    if row["claimed_epoch"] is not None
                    else "open"
                )
            metadata.pop("completion_source", None)
            metadata.pop("completion_previous_status", None)
            metadata["reopened_completion_source"] = completion_source
            updated = db.execute(
                "UPDATE surface_handoffs "
                "SET status=?,completed_epoch=NULL,metadata=? "
                "WHERE id=? AND status='completed'",
                (
                    previous,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    str(row["id"]),
                ),
            )
            changed += int(updated.rowcount)
    return changed
