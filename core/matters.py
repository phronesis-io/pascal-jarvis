"""Durable Matters — continuity above channels, models, and sessions.

A Matter is one recognizable piece of work. Existing systems keep their own
stores; this module links them through stable external IDs and records a small
append-only event trail in the shared SQLite store (core.db).
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass
from typing import Any

from core.timeutil import now_local_str

VALID_KINDS = {"project", "decision", "research", "personal", "incident"}
VALID_STATUSES = {"active", "waiting", "blocked", "done", "archived"}
VALID_ENTITY_TYPES = {
    "session", "memorial", "intent", "job", "artifact", "conversation",
    "delegation",
}
VALID_PROVIDERS = {
    "", "claude", "codex", "lark", "eigenflux", "file", "git", "github",
    "url", "jarvis",
}
UPDATABLE_FIELDS = {
    "title", "summary", "next_action", "outcome", "kind", "status",
    "priority", "source",
}
FIELD_LABELS = {
    "title": "名称",
    "summary": "当前共识",
    "next_action": "下一步",
    "outcome": "完成结果",
    "kind": "类型",
    "status": "状态",
    "priority": "优先级",
    "source": "来源",
    "closed_at": "完成时间",
}


@dataclass
class MatterConflict(ValueError):
    """A requested state transition would strand live follow-up work."""

    message: str
    open_items: list[dict]

    def __str__(self) -> str:
        return self.message


def _db():
    from core.db import get_db
    return get_db()


def _now() -> str:
    return now_local_str("%Y-%m-%dT%H:%M:%S")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _decode(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _validate_kind(kind: str) -> str:
    kind = str(kind or "project").strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid matter kind: {kind}")
    return kind


def _validate_status(status: str) -> str:
    status = str(status or "active").strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid matter status: {status}")
    return status


def _validate_priority(priority: int) -> int:
    try:
        value = int(priority)
    except (TypeError, ValueError) as exc:
        raise ValueError("priority must be an integer from 1 to 10") from exc
    if not 1 <= value <= 10:
        raise ValueError("priority must be from 1 to 10")
    return value


def _event(db, matter_id: str, event_type: str, summary: str = "",
           actor: str = "system", payload: dict | None = None,
           created_at: str | None = None) -> int:
    cur = db.execute(
        """INSERT INTO matter_events
           (matter_id, event_type, actor, summary, payload, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (matter_id, str(event_type), str(actor or "system"), str(summary or ""),
         _json(payload), created_at or _now()),
    )
    return int(cur.lastrowid)


def _matter_row(row) -> dict:
    return dict(row) if row is not None else {}


def _link_row(row) -> dict:
    item = dict(row)
    item["metadata"] = _decode(item.get("metadata"))
    return item


def _event_row(row) -> dict:
    item = dict(row)
    item["payload"] = _decode(item.get("payload"))
    return item


def create_matter(title: str, summary: str = "", next_action: str = "",
                  kind: str = "project", status: str = "active",
                  priority: int = 5, source: str = "manual",
                  actor: str = "user", matter_id: str | None = None) -> dict:
    title = str(title or "").strip()
    if not title:
        raise ValueError("matter title is required")
    kind = _validate_kind(kind)
    status = _validate_status(status)
    priority = _validate_priority(priority)
    matter_id = str(matter_id or f"mat_{uuid.uuid4().hex[:12]}")
    now = _now()
    closed_at = now if status in {"done", "archived"} else None

    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO matters
               (id, title, summary, next_action, outcome, kind, status,
                priority, source, created_at, updated_at, closed_at)
               VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)""",
            (matter_id, title, str(summary or ""), str(next_action or ""),
             kind, status, priority, str(source or "manual"), now, now, closed_at),
        )
        _event(db, matter_id, "matter_created", title, actor,
               {"kind": kind, "status": status, "priority": priority}, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_matter(matter_id)


def get_matter(matter_id: str, include_links: bool = True,
               include_events: bool = True) -> dict | None:
    db = _db()
    row = db.execute("SELECT * FROM matters WHERE id = ?", (str(matter_id),)).fetchone()
    if row is None:
        return None
    item = _matter_row(row)
    if include_links:
        rows = db.execute(
            "SELECT * FROM matter_links WHERE matter_id = ? ORDER BY updated_at DESC, id DESC",
            (matter_id,),
        ).fetchall()
        item["links"] = [_link_row(r) for r in rows]
    if include_events:
        rows = db.execute(
            "SELECT * FROM matter_events WHERE matter_id = ? ORDER BY id DESC",
            (matter_id,),
        ).fetchall()
        item["events"] = [_event_row(r) for r in rows]
    return item


def list_matters(status: str | None = None, limit: int = 100) -> list[dict]:
    db = _db()
    params: list[Any] = []
    where = ""
    if status:
        statuses = [s.strip() for s in str(status).split(",") if s.strip()]
        statuses = [_validate_status(s) for s in statuses]
        where = "WHERE m.status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    params.append(max(1, min(int(limit), 500)))
    rows = db.execute(
        f"""SELECT m.*, COUNT(l.id) AS link_count,
                   GROUP_CONCAT(DISTINCT NULLIF(l.provider, '')) AS providers
            FROM matters m LEFT JOIN matter_links l ON l.matter_id = m.id
            {where}
            GROUP BY m.id
            ORDER BY
              CASE m.status
                WHEN 'active' THEN 0 WHEN 'blocked' THEN 1
                WHEN 'waiting' THEN 2 WHEN 'done' THEN 3 ELSE 4 END,
              m.priority DESC, m.updated_at DESC
            LIMIT ?""",
        params,
    ).fetchall()
    return [_matter_row(r) for r in rows]


def open_followups(matter_id: str) -> list[dict]:
    """Return linked Intents, Memorials and Jobs that still require action."""
    matter = get_matter(matter_id, include_events=False)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    followups: list[dict] = []
    for link in matter.get("links", []):
        entity_type = link.get("entity_type", "")
        entity_id = link.get("entity_id", "")
        if entity_type == "intent":
            try:
                from core.intent_lifecycle import get_intent
                intent = get_intent(entity_id)
            except Exception:
                intent = None
            if intent and (intent.get("status") in {"pending", "triggered"}
                           or intent.get("closure_status") == "awaiting"):
                followups.append({
                    "entity_type": "intent", "entity_id": entity_id,
                    "title": intent.get("name") or link.get("title") or entity_id,
                    "status": intent.get("status", ""),
                    "closure_status": intent.get("closure_status", ""),
                })
        elif entity_type == "memorial":
            try:
                from core.memorial import get_memorial
                memorial = get_memorial(entity_id)
            except Exception:
                memorial = None
            if memorial and memorial.get("status") == "pending":
                followups.append({
                    "entity_type": "memorial", "entity_id": entity_id,
                    "title": memorial.get("title") or link.get("title") or entity_id,
                    "status": "pending",
                })
        elif entity_type == "job":
            metadata = link.get("metadata") or {}
            status = str(metadata.get("status", ""))
            try:
                from core.jobs import JobManager
                from core.config import Config
                cfg = Config()
                job = JobManager(cfg.jarvis_dir / "jobs").get_job(entity_id)
            except Exception:
                job = None
            status = str((job or {}).get("status") or status)
            if status == "running":
                followups.append({
                    "entity_type": "job", "entity_id": entity_id,
                    "title": (job or {}).get("description") or link.get("title") or entity_id,
                    "status": status,
                })
        elif entity_type == "delegation":
            try:
                from core.delegations import ACTIVE_STATUSES, DelegationStore
                delegation = DelegationStore().get(entity_id)
            except Exception:
                delegation = None
            if delegation and delegation.get("status") in ACTIVE_STATUSES:
                followups.append({
                    "entity_type": "delegation",
                    "entity_id": entity_id,
                    "title": (
                        delegation.get("title")
                        or link.get("title")
                        or entity_id
                    ),
                    "status": delegation.get("status", ""),
                })
    return followups


def update_matter(matter_id: str, actor: str = "user", force: bool = False,
                  **fields) -> dict:
    current = get_matter(matter_id, include_links=False, include_events=False)
    if current is None:
        raise KeyError(f"matter not found: {matter_id}")
    updates = {k: v for k, v in fields.items() if k in UPDATABLE_FIELDS}
    if not updates:
        return get_matter(matter_id)
    if "title" in updates:
        updates["title"] = str(updates["title"] or "").strip()
        if not updates["title"]:
            raise ValueError("matter title is required")
    for field in ("summary", "next_action", "outcome", "source"):
        if field in updates:
            updates[field] = str(updates[field] or "")
    if "kind" in updates:
        updates["kind"] = _validate_kind(updates["kind"])
    if "status" in updates:
        updates["status"] = _validate_status(updates["status"])
        if (updates["status"] in {"done", "archived"}
                and current.get("status") not in {"done", "archived"}):
            outstanding = open_followups(matter_id)
            if outstanding and not force:
                raise MatterConflict(
                    f"还有 {len(outstanding)} 项未闭环，确认后才能结束事项",
                    outstanding,
                )
        updates["closed_at"] = (_now() if updates["status"] in {"done", "archived"}
                                else None)
    if "priority" in updates:
        updates["priority"] = _validate_priority(updates["priority"])

    changes = {k: {"from": current.get(k), "to": value}
               for k, value in updates.items() if current.get(k) != value}
    if not changes:
        if current.get("status") in {"done", "archived"}:
            complete_surface_handoffs(matter_id)
        return get_matter(matter_id)
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"UPDATE matters SET {set_clause} WHERE id = ?",
                   [*updates.values(), matter_id])
        readable = "、".join(FIELD_LABELS.get(field, field) for field in changes)
        _event(db, matter_id, "matter_updated", f"更新了{readable}", actor, changes)
        if force and updates.get("status") in {"done", "archived"}:
            outstanding = open_followups(matter_id)
            if outstanding:
                _event(db, matter_id, "matter_closed_with_followups",
                       f"确认保留 {len(outstanding)} 项未闭环内容", actor,
                       {"items": outstanding})
        db.commit()
    except Exception:
        db.rollback()
        raise
    if updates.get("status") in {"done", "archived"}:
        complete_surface_handoffs(matter_id)
    return get_matter(matter_id)


def complete_surface_handoffs(matter_id: str) -> None:
    """Best-effort convergence for phone/desktop continuation affordances."""
    try:
        from core.continuity import complete_entity_handoffs
        complete_entity_handoffs("matter", matter_id)
    except Exception:
        pass


def add_event(matter_id: str, event_type: str, summary: str = "",
              actor: str = "system", payload: dict | None = None) -> dict:
    if get_matter(matter_id, include_links=False, include_events=False) is None:
        raise KeyError(f"matter not found: {matter_id}")
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        event_id = _event(db, matter_id, event_type, summary, actor, payload)
        db.execute("UPDATE matters SET updated_at = ? WHERE id = ?", (_now(), matter_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = db.execute("SELECT * FROM matter_events WHERE id = ?", (event_id,)).fetchone()
    return _event_row(row)


def link_entity(matter_id: str, entity_type: str, entity_id: str,
                provider: str = "", title: str = "", metadata: dict | None = None,
                actor: str = "user", move: bool = False) -> dict:
    matter = get_matter(matter_id, include_links=False, include_events=False)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    entity_type = str(entity_type or "").strip().lower()
    provider = str(provider or "").strip().lower()
    entity_id = str(entity_id or "").strip()
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"invalid entity type: {entity_type}")
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"invalid provider: {provider}")
    if not entity_id:
        raise ValueError("entity_id is required")
    now = _now()
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT * FROM matter_links "
            "WHERE provider = ? AND entity_type = ? AND entity_id = ?",
            (provider, entity_type, entity_id),
        ).fetchone()
        if existing is not None:
            old = dict(existing)
            if old["matter_id"] != matter_id and not move:
                raise ValueError(
                    f"{provider}:{entity_type}:{entity_id} is already linked to "
                    f"{old['matter_id']}; pass move=True to move it")
            if old["matter_id"] != matter_id:
                db.execute(
                    "UPDATE matter_links SET matter_id = ?, title = ?, metadata = ?, updated_at = ? "
                    "WHERE id = ?",
                    (matter_id, str(title or old.get("title", "")),
                     _json(metadata if metadata is not None else _decode(old.get("metadata"))), now,
                     old["id"]),
                )
                _event(db, old["matter_id"], "link_moved_out", str(title or entity_id),
                       actor, {"link_id": old["id"], "to": matter_id})
                _event(db, matter_id, "link_moved_in", str(title or entity_id), actor,
                       {"link_id": old["id"], "from": old["matter_id"]})
                link_id = old["id"]
            else:
                db.execute(
                    "UPDATE matter_links SET title = ?, metadata = ?, updated_at = ? WHERE id = ?",
                    (str(title or old.get("title", "")),
                     _json(metadata if metadata is not None else _decode(old.get("metadata"))),
                     now, old["id"]),
                )
                link_id = old["id"]
        else:
            cur = db.execute(
                """INSERT INTO matter_links
                   (matter_id, entity_type, provider, entity_id, title, metadata,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (matter_id, entity_type, provider, entity_id, str(title or ""),
                 _json(metadata), now, now),
            )
            link_id = int(cur.lastrowid)
            _event(db, matter_id, "link_added", str(title or entity_id), actor,
                   {"link_id": link_id, "entity_type": entity_type,
                    "provider": provider, "entity_id": entity_id})
        db.execute("UPDATE matters SET updated_at = ? WHERE id = ?", (now, matter_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = db.execute("SELECT * FROM matter_links WHERE id = ?", (link_id,)).fetchone()
    return _link_row(row)


def unlink_entity(matter_id: str, link_id: int, actor: str = "user") -> bool:
    db = _db()
    row = db.execute(
        "SELECT * FROM matter_links WHERE id = ? AND matter_id = ?",
        (int(link_id), matter_id),
    ).fetchone()
    if row is None:
        return False
    link = dict(row)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM matter_links WHERE id = ?", (int(link_id),))
        _event(db, matter_id, "link_removed", link.get("title") or link["entity_id"],
               actor, {"link_id": int(link_id), "entity_type": link["entity_type"],
                       "provider": link["provider"], "entity_id": link["entity_id"]})
        db.execute("UPDATE matters SET updated_at = ? WHERE id = ?", (_now(), matter_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True


def find_by_entity(entity_type: str, entity_id: str,
                   provider: str = "") -> dict | None:
    db = _db()
    row = db.execute(
        """SELECT m.* FROM matters m JOIN matter_links l ON l.matter_id = m.id
           WHERE l.entity_type = ? AND l.entity_id = ? AND l.provider = ?""",
        (str(entity_type), str(entity_id), str(provider)),
    ).fetchone()
    return _matter_row(row) if row is not None else None


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.matters")
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create")
    create.add_argument("title")
    create.add_argument("--summary", default="")
    create.add_argument("--next-action", default="")
    create.add_argument("--kind", default="project", choices=sorted(VALID_KINDS))
    create.add_argument("--priority", type=int, default=5)
    listing = sub.add_parser("list")
    listing.add_argument("--status", default="")
    show = sub.add_parser("show")
    show.add_argument("matter_id")
    link = sub.add_parser("link-session")
    link.add_argument("matter_id")
    link.add_argument("provider", choices=("claude", "codex"))
    link.add_argument("session_id")
    link.add_argument("--title", default="")
    args = parser.parse_args(argv)

    if args.cmd == "create":
        _print(create_matter(args.title, args.summary, args.next_action,
                             args.kind, priority=args.priority))
    elif args.cmd == "list":
        _print(list_matters(args.status or None))
    elif args.cmd == "show":
        matter = get_matter(args.matter_id)
        if matter is None:
            parser.error(f"matter not found: {args.matter_id}")
        _print(matter)
    elif args.cmd == "link-session":
        _print(link_entity(args.matter_id, "session", args.session_id,
                           provider=args.provider, title=args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
