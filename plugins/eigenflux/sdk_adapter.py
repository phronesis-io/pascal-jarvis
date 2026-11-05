"""Opt-in SDK consumer for an explicitly isolated Jarvis database.

Feed becomes searchable Jarvis bookmarks, not notifications or executable work.
The SDK owns polling/retry; SQLite receipts prove only durable local acceptance.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from eigenflux_sdk import DeliveryResult

from core import db as jarvis_db
from core.timeutil import now_local_str


def read_context(path: Path, root: Path) -> dict:
    path = path.resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError("context must be inside the isolated runtime root")
    raw = path.read_bytes()
    if len(raw) > 16384:
        raise ValueError("context exceeds 16 KiB")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) not in ({"summary"}, {"fields"}):
        raise ValueError("context must contain summary or fields")
    if "summary" in value and (not isinstance(value["summary"], str) or len(value["summary"]) > 4000):
        raise ValueError("invalid summary")
    if "fields" in value and not isinstance(value["fields"], dict):
        raise ValueError("fields must be an object")
    return value


def fingerprint(delivery) -> str:
    encoded = json.dumps(delivery.feed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class JarvisInbox:
    def __init__(self, root: Path):
        expected = root.resolve() / "data" / "jarvis.db"
        if jarvis_db._db_path().resolve() != expected:
            raise ValueError("Jarvis database does not match the isolated runtime root")
        with jarvis_db.independent_connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS eigenflux_sdk_receipts (
                    batch_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, payload_hash TEXT NOT NULL,
                    receipt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eigenflux_sdk_items (
                    agent_id TEXT NOT NULL, item_id TEXT NOT NULL,
                    bookmark_id INTEGER NOT NULL REFERENCES bookmarks(id),
                    PRIMARY KEY(agent_id,item_id)
                );
                CREATE TABLE IF NOT EXISTS eigenflux_sdk_payloads (
                    batch_id TEXT PRIMARY KEY REFERENCES eigenflux_sdk_receipts(batch_id),
                    feed_json TEXT NOT NULL
                );
            """)
        expected.chmod(0o600)

    @staticmethod
    def _receipt(db, delivery):
        row = db.execute("SELECT * FROM eigenflux_sdk_receipts WHERE batch_id=?", (delivery.batch_id,)).fetchone()
        if row and (row["agent_id"] != delivery.agent_id or row["sequence"] != delivery.sequence
                    or row["payload_hash"] != fingerprint(delivery)):
            raise ValueError("batch identity conflict")
        return row

    def push_to_agent(self, delivery):
        if delivery.cancelled:
            return DeliveryResult.busy()
        try:
            with jarvis_db.independent_connection() as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                row = self._receipt(db, delivery)
                if row:
                    db.execute("INSERT OR IGNORE INTO eigenflux_sdk_payloads VALUES(?,?)",
                               (delivery.batch_id, json.dumps(delivery.feed)))
                    db.commit()
                    return DeliveryResult.accepted(row["receipt"])
                for item in delivery.items:
                    item_id = str(item["item_id"])
                    exists = db.execute("SELECT 1 FROM eigenflux_sdk_items WHERE agent_id=? AND item_id=?",
                                        (delivery.agent_id, item_id)).fetchone()
                    if exists:
                        continue
                    stamp = now_local_str("%Y-%m-%dT%H:%M:%S")
                    cur = db.execute("""INSERT INTO bookmarks
                        (title,url,source,summary,tags,content,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?)""", (
                            str(item.get("title") or "EigenFlux Feed"), str(item.get("url") or ""),
                            "eigenflux-sdk", str(item.get("summary") or ""),
                            json.dumps(item.get("keywords") or []), json.dumps(item), stamp, stamp))
                    db.execute("INSERT INTO eigenflux_sdk_items VALUES(?,?,?)",
                               (delivery.agent_id, item_id, cur.lastrowid))
                if delivery.cancelled:
                    db.rollback()
                    return DeliveryResult.busy()
                receipt = "jarvis-bookmarks:" + delivery.batch_id
                db.execute("INSERT INTO eigenflux_sdk_receipts VALUES(?,?,?,?,?)", (
                    delivery.batch_id, delivery.agent_id, delivery.sequence, fingerprint(delivery), receipt))
                db.execute("INSERT INTO eigenflux_sdk_payloads VALUES(?,?)",
                           (delivery.batch_id, json.dumps(delivery.feed)))
                db.commit()
            return DeliveryResult.accepted(receipt)
        except Exception:
            return DeliveryResult.unknown()

    def delivery_status(self, delivery):
        try:
            with jarvis_db.independent_connection() as db:
                row = self._receipt(db, delivery)
            return DeliveryResult.accepted(row["receipt"]) if row else DeliveryResult.not_accepted()
        except Exception:
            return DeliveryResult.unknown()

    def snapshot(self):
        with jarvis_db.independent_connection() as db:
            batches = [dict(row) for row in db.execute(
                "SELECT batch_id,agent_id,sequence FROM eigenflux_sdk_receipts ORDER BY sequence")]
            count = db.execute("SELECT count(*) FROM eigenflux_sdk_items").fetchone()[0]
        return {"accepted_batches": len(batches), "bookmarks": count, "batches": batches}
