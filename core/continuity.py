"""Durable cross-device handoffs for existing Jarvis work objects.

A handoff moves the next interaction between desktop and phone. It never
copies the Memorial or Matter it points at; those stores remain authoritative.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

SURFACES = {"desktop", "mobile"}
ENTITY_TYPES = {"memorial", "matter"}
ACTIVE_STATES = {"open", "claimed"}
TERMINAL_STATES = {"completed", "cancelled"}


def _db():
    from dashboard.db import get_db
    return get_db()


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
        raise ValueError("entity_type must be memorial or matter")
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
    else:
        from core.matters import get_matter
        exists = get_matter(
            entity_id, include_links=False, include_events=False) is not None
    if not exists:
        raise KeyError(entity_id)


def _database_path() -> Path | None:
    try:
        rows = _db().execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        if str(row["name"]) == "main" and row["file"]:
            return Path(str(row["file"]))
    return None


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
    matter_id = str(handoff.get("matter_id") or (
        entity_id if is_matter else ""))
    result = pipeline.deliver(DeliveryEnvelope(
        source="surface-handoff",
        kind="push",
        payload={
            "title": "Jarvis · 发到手机",
            "text": title,
            "url": (
                f"/items/{entity_id}"
                if not is_matter
                else f"/matters/{entity_id}"
            ),
            "matter_id": matter_id,
        },
        attention="reply",
        requested_channel="push",
        urgent=True,
        conversation_bound=True,
        memorial_id=entity_id if not is_matter else "",
        matter_id=matter_id,
        dedup_key=f"surface-handoff:{handoff['id']}",
        metadata={
            "bypass_quiet": True,
            "bypass_throttle": True,
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
    db = _db()
    now = float(clock())
    existing = db.execute(
        "SELECT * FROM surface_handoffs WHERE entity_type=? AND entity_id=? "
        "AND to_surface=? AND status IN ('open','claimed') "
        "ORDER BY created_epoch DESC LIMIT 1",
        (entity_type, entity_id, to_surface),
    ).fetchone()
    if existing:
        return {**_decode(existing), "created": False}

    handoff_id = f"hop_{uuid.uuid4().hex}"
    try:
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
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        existing = db.execute(
            "SELECT * FROM surface_handoffs WHERE entity_type=? AND entity_id=? "
            "AND to_surface=? AND status IN ('open','claimed') "
            "ORDER BY created_epoch DESC LIMIT 1",
            (entity_type, entity_id, to_surface),
        ).fetchone()
        if existing:
            return {**_decode(existing), "created": False}
        raise

    row = db.execute(
        "SELECT * FROM surface_handoffs WHERE id=?", (handoff_id,)
    ).fetchone()
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
            db.execute(
                "UPDATE surface_handoffs SET delivery_id=? WHERE id=?",
                (delivery["delivery_id"], handoff_id),
            )
            db.commit()
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
    rows = _db().execute(
        f"SELECT * FROM surface_handoffs{where} "
        "ORDER BY created_epoch DESC LIMIT ?",
        params,
    ).fetchall()
    return [_decode(row) for row in rows]


def get_handoff(handoff_id: str) -> dict | None:
    row = _db().execute(
        "SELECT * FROM surface_handoffs WHERE id=?", (str(handoff_id),)
    ).fetchone()
    return _decode(row) if row else None


def claim_handoff(
    handoff_id: str,
    *,
    surface: str,
    clock=time.time,
) -> dict:
    if surface not in SURFACES:
        raise ValueError("surface must be desktop or mobile")
    db = _db()
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
        db.commit()
    return get_handoff(handoff_id)


def complete_handoff(handoff_id: str, *, clock=time.time) -> dict:
    db = _db()
    row = db.execute(
        "SELECT * FROM surface_handoffs WHERE id=?", (str(handoff_id),)
    ).fetchone()
    if not row:
        raise KeyError(handoff_id)
    if str(row["status"]) in ACTIVE_STATES:
        db.execute(
            "UPDATE surface_handoffs SET status='completed',completed_epoch=? "
            "WHERE id=? AND status IN ('open','claimed')",
            (float(clock()), str(handoff_id)),
        )
        db.commit()
    return get_handoff(handoff_id)


def complete_entity_handoffs(
    entity_type: str,
    entity_id: str,
    *,
    clock=time.time,
) -> int:
    if entity_type not in ENTITY_TYPES:
        raise ValueError("entity_type must be memorial or matter")
    db = _db()
    changed = db.execute(
        "UPDATE surface_handoffs SET status='completed',completed_epoch=? "
        "WHERE entity_type=? AND entity_id=? "
        "AND status IN ('open','claimed')",
        (float(clock()), entity_type, str(entity_id)),
    ).rowcount
    db.commit()
    return int(changed)
