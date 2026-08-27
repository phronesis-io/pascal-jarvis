"""Discover private conversation sources and create replayable compile batches."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from core.memory_compiler_common import (
    DEFAULT_BATCH_SIZE,
    INPUT_SCHEMA,
    MAX_CLAIMS_PER_SOURCE,
    OUTPUT_SCHEMA,
    SOURCE_SCAN_PAGE_SIZE,
    VALID_KINDS,
    db,
    decode,
    digest,
    flat,
    json_text,
    now as current_epoch,
    source_activation_policy,
)


def _source_ref(prefix: str, identity: Any) -> str:
    return f"{prefix}:{str(identity or '').strip()}"


def _known_source_refs() -> set[str]:
    return {
        str(row[0]) for row in db().execute(
            "SELECT source_ref FROM memory_compile_sources"
        )
    }


def _linked_session_matter(provider: str, session_id: str) -> str:
    if not session_id:
        return ""
    row = db().execute(
        """SELECT matter_id FROM matter_links
             WHERE entity_type='session' AND provider=? AND entity_id=?""",
        (provider, session_id),
    ).fetchone()
    return str(row[0] or "") if row else ""


def _session_sources(
    *, root: str | Path | None = None, index_db: str | Path | None = None,
    known: set[str], limit: int,
) -> list[dict[str, Any]]:
    from core.cross_session_index import _db_path, _read_connect

    path = _db_path(index_db, root)
    if not path.exists():
        return []
    connection = _read_connect(path)
    sources: list[dict[str, Any]] = []
    cursor: tuple[str, str] | None = None
    try:
        while len(sources) < limit:
            if cursor is None:
                rows = connection.execute(
                    """SELECT identity,provider,session_id,workspace,role,
                              occurred_at,text
                         FROM session_turns
                        ORDER BY occurred_at DESC, identity DESC LIMIT ?""",
                    (SOURCE_SCAN_PAGE_SIZE,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT identity,provider,session_id,workspace,role,
                              occurred_at,text
                         FROM session_turns
                        WHERE occurred_at < ?
                           OR (occurred_at = ? AND identity < ?)
                        ORDER BY occurred_at DESC, identity DESC LIMIT ?""",
                    (cursor[0], cursor[0], cursor[1], SOURCE_SCAN_PAGE_SIZE),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                source_ref = _source_ref("session_turn", row["identity"])
                if source_ref in known:
                    continue
                text = flat(row["text"])
                if not text:
                    continue
                provider = str(row["provider"] or "")
                session_id = str(row["session_id"] or "")
                role = str(row["role"] or "")
                sources.append({
                    "source_ref": source_ref,
                    "source_kind": "session_turn",
                    "provider": provider,
                    "role": role,
                    "activation_policy": source_activation_policy(role, text),
                    "occurred_at": str(row["occurred_at"] or ""),
                    "matter_id": _linked_session_matter(provider, session_id),
                    "text": text,
                    "metadata": {
                        "session_id": session_id,
                        "workspace": str(row["workspace"] or "")[:120],
                    },
                })
                if len(sources) >= limit:
                    break
            last = rows[-1]
            cursor = (str(last["occurred_at"] or ""), str(last["identity"] or ""))
            if len(rows) < SOURCE_SCAN_PAGE_SIZE:
                break
    finally:
        connection.close()
    return sources


def _lark_sources(*, known: set[str], limit: int) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    before_id: int | None = None
    while len(sources) < limit:
        if before_id is None:
            rows = db().execute(
                """SELECT id,role,text,message_id,provider,session_id,created_at,
                          matter_id,context_key
                     FROM conversation_turns
                    WHERE memory_eligible=1
                    ORDER BY id DESC LIMIT ?""",
                (SOURCE_SCAN_PAGE_SIZE,),
            ).fetchall()
        else:
            rows = db().execute(
                """SELECT id,role,text,message_id,provider,session_id,created_at,
                          matter_id,context_key
                     FROM conversation_turns
                    WHERE memory_eligible=1 AND id < ?
                    ORDER BY id DESC LIMIT ?""",
                (before_id, SOURCE_SCAN_PAGE_SIZE),
            ).fetchall()
        if not rows:
            break
        for row in rows:
            source_ref = _source_ref("lark_turn", row["id"])
            if source_ref in known:
                continue
            text = flat(row["text"])
            if not text:
                continue
            role = str(row["role"] or "")
            sources.append({
                "source_ref": source_ref,
                "source_kind": "lark_turn",
                "provider": str(row["provider"] or "lark"),
                "role": role,
                "activation_policy": source_activation_policy(role, text),
                "occurred_at": str(row["created_at"] or ""),
                "matter_id": str(row["matter_id"] or ""),
                "text": text,
                "metadata": {
                    "message_id": str(row["message_id"] or ""),
                    "session_id": str(row["session_id"] or ""),
                    "context_key": str(row["context_key"] or "")[:300],
                },
            })
            if len(sources) >= limit:
                break
        before_id = int(rows[-1]["id"])
        if len(rows) < SOURCE_SCAN_PAGE_SIZE:
            break
    return sources


def _pending_batch() -> dict[str, Any] | None:
    row = db().execute(
        """SELECT * FROM memory_compile_batches WHERE status='pending'
           ORDER BY created_epoch LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["payload"] = decode(result.get("payload"), {})
    result["source_refs"] = decode(result.get("source_refs"), [])
    return result


def prepare_batch(
    *, root: str | Path | None = None, index_db: str | Path | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE, now: float | None = None,
) -> dict[str, Any] | None:
    """Create or replay one private compile batch without losing source turns."""
    existing = _pending_batch()
    if existing:
        return existing["payload"]
    size = max(1, min(int(batch_size), 64))
    known = _known_source_refs()
    session_candidates = _session_sources(
        root=root, index_db=index_db, known=known, limit=size,
    )
    lark_candidates = _lark_sources(known=known, limit=size)
    session_quota = (size + 1) // 2
    lark_quota = size // 2
    sessions = session_candidates[:session_quota]
    lark = lark_candidates[:lark_quota]
    remaining = size - len(sessions) - len(lark)
    if remaining:
        sessions.extend(session_candidates[len(sessions):len(sessions) + remaining])
        remaining = size - len(sessions) - len(lark)
    if remaining:
        lark.extend(lark_candidates[len(lark):len(lark) + remaining])
    sources = sorted(
        [*sessions, *lark],
        key=lambda item: (item.get("occurred_at", ""), item["source_ref"]),
    )
    if not sources:
        return None
    batch_id = f"mcb_{uuid.uuid4().hex[:20]}"
    payload = {
        "schema": INPUT_SCHEMA,
        "batch_id": batch_id,
        "contract": {
            "output_schema": OUTPUT_SCHEMA,
            "quote": "exact non-empty substring of source text",
            "claim_kinds": sorted(VALID_KINDS),
            "max_claims_per_source": MAX_CLAIMS_PER_SOURCE,
            "matter_binding": "copy the source matter_id only; otherwise empty",
            "completion": "assistant completion prose is never evidence",
            "context_dependent_owner_turns": (
                "owner_context_candidate sources can never auto-activate"
            ),
            "claim_activation": (
                "core re-evaluates the exact quote; a contextual quote is candidate-only"
            ),
            "coverage": "every source_ref must be claimed or ignored",
        },
        "sources": sources,
    }
    epoch = current_epoch(now)
    connection = db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO memory_compile_batches
               (id,status,source_refs,payload,created_epoch)
               VALUES (?,'pending',?,?,?)""",
            (batch_id, json_text([item["source_ref"] for item in sources]),
             json_text(payload), epoch),
        )
        connection.executemany(
            """INSERT INTO memory_compile_sources
               (source_ref,source_kind,provider,role,occurred_at,source_digest,
                matter_id,status,batch_id,metadata)
               VALUES (?,?,?,?,?,?,NULLIF(?,''),'pending',?,?)""",
            (
                (
                    item["source_ref"], item["source_kind"], item["provider"],
                    item["role"], item["occurred_at"], digest(item["text"]),
                    item["matter_id"], batch_id, json_text(item["metadata"]),
                )
                for item in sources
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return payload
