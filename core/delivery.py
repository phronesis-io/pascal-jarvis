"""One delivery pipeline for every Jarvis user-facing output.

All producers hand this module a :class:`DeliveryEnvelope`.  The pipeline
owns the policy and state transition order:

    sanitize -> dedup -> throttle -> quiet-hours -> route -> send -> confirm

The durable state lives in ``data/jarvis.db`` under SQLite WAL.  A producer
does not maintain its own retry, queue, or dedup file.  Legacy helpers may
remain as compatibility adapters, but their decisions terminate here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from core.attention_policy import in_quiet_hours, next_awake
from core.card import extract_card_text
from core.log import log
from core.runtime_paths import database_path
from core.timeutil import now_local

DELIVERY_STATES = {
    "queued", "attempting", "delivered", "read", "acted", "suppressed",
    "failed",
}
FINAL_SUCCESS_STATES = {"delivered", "read", "acted"}
RETRY_DELAYS = (0, 2, 5)
MAX_DELIVERY_ATTEMPTS = 9
SEND_TIMEOUT_SECONDS = 15
ATTEMPT_STALE_SECONDS = 90
DEDUP_WINDOW_SECONDS = 6 * 3600
DEFAULT_SOURCE_DAILY_CAP = 24
DEFAULT_GLOBAL_DAILY_CAP = 25
DEFAULT_BURST_CAP = 4
DEFAULT_BURST_WINDOW_SECONDS = 10 * 60
CAP_RESERVATION_RECHECK_SECONDS = 5

_STATE_UPDATE_FIELDS = frozenset({
    "attempts",
    "delivered_epoch",
    "next_attempt_epoch",
    "last_error",
    "message_id",
    "read_epoch",
    "acted_epoch",
})
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: dict[Path, tuple[int, int]] = {}


def _ops_log(message: str, *, level: str = "info", **fields) -> None:
    try:
        log("delivery", message, level=level, **fields)
    except Exception:
        pass

_TOOL_NARRATION_RE = re.compile(
    r"^\s*(?:🔧\s*)?(?:"
    r"I(?:'ll| will| am going to)\b|Let me\b|Now I(?:'ll| will)\b|"
    r"Running\b|Reading\b|Searching\b|Writing\b|Using (?:the )?\w+ tool\b|"
    r"Tool (?:call|result)\b|Execution error\b|"
    r"<\/?(?:tool|function|analysis)[^>]*>"
    r")",
    re.IGNORECASE,
)
_INTERNAL_LINE_RE = re.compile(
    r"^\s*(?:"
    r"===\s*TASK\b.*===|"
    r"\[(?:CHECKIN|DAILY PLAN|SYSTEM|TOOL|ANALYSIS|INTERNAL)\]|"
    r"(?:assistant|analysis|tool)\s+to=\w+"
    r")\s*$",
    re.IGNORECASE,
)
_DYNAMIC_ID_RE = re.compile(
    r"\b(?:mem|delivery|run|job)_[A-Za-z0-9_.:-]+\b", re.IGNORECASE)


@dataclass(slots=True)
class DeliveryEnvelope:
    source: str
    kind: str = "text"
    payload: dict = field(default_factory=dict)
    attention: str = "notice"
    requested_channel: str = "auto"
    urgent: bool = False
    conversation_bound: bool = False
    reply_to: str = ""
    chat_id: str = ""
    memorial_id: str = ""
    matter_id: str = ""
    dedup_key: str = ""
    throttle_key: str = ""
    provider: str = ""
    model: str = ""
    metadata: dict = field(default_factory=dict)
    id: str = ""

    def normalized(self) -> "DeliveryEnvelope":
        self.source = str(self.source or "unknown").strip() or "unknown"
        self.kind = str(self.kind or "text").strip()
        self.attention = str(self.attention or "notice").strip()
        self.requested_channel = str(
            self.requested_channel or "auto").strip()
        self.payload = dict(self.payload or {})
        self.metadata = dict(self.metadata or {})
        if not self.id:
            self.id = f"dlv_{uuid.uuid4().hex}"
        # "web" is deliberately NOT a kind: the web surface is retired
        # (REQ-119) and its transport used to fake success unconditionally.
        # "push" is not a kind either: the mobile gateway and Web Push are
        # retired (REQ-120) — Lark is the only delivery surface.
        if self.kind not in {"text", "card", "reply"}:
            raise ValueError(f"unsupported delivery kind: {self.kind}")
        if self.attention not in {"decision", "notice", "alert", "reply"}:
            raise ValueError(f"unsupported attention: {self.attention}")
        return self


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivery_id: str
    accepted: bool
    state: str
    channel: str = ""
    message_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TransportResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    suppressed_reason: str = ""


Transport = Callable[[DeliveryEnvelope, str], TransportResult]


def _root(value: str | Path | None = None) -> Path:
    if value:
        return Path(value)
    return Path(os.environ.get("JARVIS_DIR")
                or Path(__file__).resolve().parent.parent)


def _db_path(root: Path, explicit: str | Path | None = None) -> Path:
    return database_path(root, explicit)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        resolved = path.resolve()
        with _SCHEMA_LOCK:
            try:
                stat = resolved.stat()
                identity = (int(stat.st_dev), int(stat.st_ino))
            except OSError:
                identity = (-1, -1)
            if _SCHEMA_READY.get(resolved) != identity:
                _ensure_schema(db)
                stat = resolved.stat()
                _SCHEMA_READY[resolved] = (
                    int(stat.st_dev), int(stat.st_ino))
        return db
    except Exception:
        db.close()
        raise


def _ensure_schema(db: sqlite3.Connection) -> None:
    # Kept here as well as dashboard.db's migration: bot/daemon may be the
    # first process after an upgrade and cannot depend on the UI starting.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS delivery_envelopes (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
            attention TEXT NOT NULL DEFAULT 'notice',
            requested_channel TEXT NOT NULL DEFAULT 'auto',
            route_channel TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'queued',
            content_hash TEXT NOT NULL, dedup_key TEXT NOT NULL DEFAULT '',
            throttle_key TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}', metadata TEXT NOT NULL DEFAULT '{}',
            provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            memorial_id TEXT NOT NULL DEFAULT '', matter_id TEXT NOT NULL DEFAULT '',
            reply_to TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', created_epoch REAL NOT NULL,
            updated_epoch REAL NOT NULL, next_attempt_epoch REAL,
            delivered_epoch REAL, read_epoch REAL, acted_epoch REAL,
            message_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id TEXT NOT NULL, attempt INTEGER NOT NULL,
            channel TEXT NOT NULL, started_epoch REAL NOT NULL,
            finished_epoch REAL, status TEXT NOT NULL DEFAULT 'attempting',
            error TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS delivery_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT NOT NULL,
            state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            created_epoch REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS delivery_dead_letters (
            id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT NOT NULL,
            source TEXT NOT NULL, kind TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', created_epoch REAL NOT NULL,
            notified_epoch REAL
        );
        CREATE TABLE IF NOT EXISTS delivery_cap_reservations (
            delivery_id TEXT PRIMARY KEY, day_start_epoch REAL NOT NULL,
            throttle_key TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', reserved_epoch REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_dedup
            ON delivery_envelopes(content_hash, created_epoch DESC);
        CREATE INDEX IF NOT EXISTS idx_delivery_queue
            ON delivery_envelopes(state, next_attempt_epoch);
        CREATE INDEX IF NOT EXISTS idx_delivery_source_day
            ON delivery_envelopes(source, created_epoch DESC);
        CREATE INDEX IF NOT EXISTS idx_delivery_deadletter_pending
            ON delivery_dead_letters(notified_epoch, created_epoch);
        CREATE INDEX IF NOT EXISTS idx_delivery_cap_reservation_day
            ON delivery_cap_reservations(day_start_epoch, source, throttle_key);
    """)
    db.commit()


def _resolve_user_id(root: Path) -> str:
    value = (os.environ.get("USER_ID")
             or os.environ.get("LARK_USER_ID") or "").strip()
    if value:
        return value
    try:
        from core.config import Config
        return str(Config(root / "jarvis.yaml").lark.get("user_id", "") or "")
    except Exception as exc:
        _ops_log(
            "lark_user_resolution_failed",
            level="error",
            error_type=type(exc).__name__,
        )
        return ""


def _extract_message_id(stdout: str) -> str:
    try:
        obj = json.loads(stdout or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    return str(
        obj.get("message_id") or data.get("message_id")
        or message.get("message_id") or "")


def _default_transport(root: Path) -> Transport:
    def send(envelope: DeliveryEnvelope, channel: str) -> TransportResult:
        # No "web" branch on purpose (REQ-119): it returned unconditional
        # success for a surface with 1.8% read rate — a fake ledger of
        # "delivered" cards nobody saw. No "push" branch either: the mobile
        # gateway and Web Push are retired (REQ-120). An unknown channel is
        # refused below; a defensive branch may not fabricate a receipt.
        if channel not in {"lark", "lark_reply"}:
            return TransportResult(False, error=f"unknown channel: {channel}")
        user_id = _resolve_user_id(root)
        if channel == "lark_reply":
            if not envelope.reply_to:
                return TransportResult(False, error="missing reply_to")
            command = [
                "lark-cli", "im", "+messages-reply",
                "--message-id", envelope.reply_to,
                "--markdown", str(envelope.payload.get("text") or ""),
                "--as", "bot", "--json",
            ]
        else:
            target: list[str]
            if envelope.chat_id:
                target = ["--chat-id", envelope.chat_id]
            elif user_id:
                target = ["--user-id", user_id]
            else:
                return TransportResult(False, error="missing Lark user id")
            if envelope.kind == "card":
                command = [
                    "lark-cli", "im", "+messages-send", *target,
                    "--msg-type", "interactive",
                    "--content", str(envelope.payload.get("card_json") or ""),
                    "--as", "bot", "--json",
                ]
            else:
                command = [
                    "lark-cli", "im", "+messages-send", *target,
                    "--markdown", str(envelope.payload.get("text") or ""),
                    "--as", "bot", "--json",
                ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=SEND_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            return TransportResult(False, error="send timed out after 15s")
        except Exception as exc:
            _ops_log(
                "lark_transport_exception",
                level="error",
                delivery_id=envelope.id,
                channel=channel,
                error_type=type(exc).__name__,
            )
            return TransportResult(False, error=str(exc))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "lark-cli failed").strip()
            return TransportResult(False, error=detail[:500])
        return TransportResult(True, _extract_message_id(result.stdout))
    return send


def _sanitize_text(value: str, proactive: bool) -> tuple[str, str]:
    from core.safety import (
        looks_like_error,
        plain_user_copy,
        sentinel_present,
        strip_task_framing,
    )

    raw = str(value or "")
    if sentinel_present(raw):
        return "", "idle_sentinel"
    stripped = strip_task_framing(raw)
    kept = []
    removed = False
    for line in stripped.splitlines():
        if _INTERNAL_LINE_RE.match(line) or _TOOL_NARRATION_RE.match(line):
            removed = True
            continue
        kept.append(line)
    text = "\n".join(kept).strip()
    if proactive:
        text = plain_user_copy(text)
    if not text:
        return "", "tool_narration" if removed else "empty"
    if looks_like_error(text, proactive=proactive):
        return "", "error_surface"
    # A complete machine envelope is never user copy.
    if proactive and text[:1] in "[{" and text[-1:] in "]}":
        try:
            json.loads(text)
            return "", "raw_json"
        except (json.JSONDecodeError, ValueError):
            pass
    return text, ""


def _sanitize_card(card_json: str, proactive: bool) -> tuple[str, str, str]:
    try:
        card = json.loads(card_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return "", "", "invalid_card"

    blocked = ""

    def walk(value):
        nonlocal blocked
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key == "value":
                    # Interactive-card callback payloads are structured data,
                    # not user-visible copy. Mutating them can change the
                    # action that runs or the value it persists.
                    continue
                if key in {"content", "text", "title"} and isinstance(item, str):
                    clean, reason = _sanitize_text(item, proactive)
                    if reason and not clean:
                        blocked = blocked or reason
                    value[key] = clean
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(card)
    if blocked:
        return "", "", blocked
    compact = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    try:
        from core.card import extract_card_text
        readable = extract_card_text(compact)
    except Exception as exc:
        _ops_log(
            "card_text_extraction_failed",
            level="warn",
            error_type=type(exc).__name__,
        )
        readable = ""
    if not readable:
        fragments: list[str] = []

        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "value":
                        continue
                    if key in {"content", "text", "title"} and isinstance(item, str):
                        if item.strip():
                            fragments.append(item.strip())
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(card)
        readable = "\n".join(fragments)
    clean_readable, reason = _sanitize_text(readable, proactive)
    if reason:
        return "", "", reason
    return compact, clean_readable, ""


def sanitize(envelope: DeliveryEnvelope) -> tuple[DeliveryEnvelope, str]:
    """Return a sanitized copy in-place and a suppression reason."""
    proactive = envelope.attention != "reply" and envelope.kind != "reply"
    if envelope.kind == "card":
        card, readable, reason = _sanitize_card(
            str(envelope.payload.get("card_json") or ""), proactive)
        if reason:
            return envelope, reason
        envelope.payload["card_json"] = card
        envelope.payload["text"] = readable
        return envelope, ""
    text, reason = _sanitize_text(
        str(envelope.payload.get("text") or ""), proactive)
    envelope.payload["text"] = text
    return envelope, reason


def _content_hash(envelope: DeliveryEnvelope) -> str:
    value = str(envelope.metadata.get("dedup_text")
                or envelope.payload.get("text")
                or envelope.payload.get("card_json") or "")
    value = _DYNAMIC_ID_RE.sub("<id>", value)
    value = " ".join(value.lower().split())
    scope = envelope.reply_to if envelope.kind == "reply" else ""
    return hashlib.sha256(f"{scope}\0{value}".encode("utf-8")).hexdigest()


def _route(envelope: DeliveryEnvelope) -> str:
    requested = envelope.requested_channel
    if requested != "auto":
        # An explicit "web"/"phone"/"push"/"mobile"/"none" request passes
        # through unmapped and is refused by the transport (REQ-119/REQ-120):
        # the web desk and the phone push surface are retired, and silently
        # rerouting an explicit request would hide the dead caller.
        aliases = {"lark_card": "lark", "reply": "lark_reply"}
        return aliases.get(requested, requested)
    if envelope.kind == "reply" or envelope.reply_to:
        return "lark_reply"
    # Lark is the only delivery surface (REQ-119). Producers that want no
    # realtime delivery stay out of the pipeline (memorial ledger-only cards).
    return "lark"


def _quiet_now(moment: datetime) -> bool:
    return in_quiet_hours(moment.hour * 60 + moment.minute)


def _next_awake_epoch(moment: datetime) -> float:
    return next_awake(moment).timestamp()


def _budget_exempt(envelope: DeliveryEnvelope) -> bool:
    """True for traffic that must not wait behind proactive-card budgets."""
    return bool(
        envelope.metadata.get("bypass_throttle")
        or envelope.kind == "reply"
        or envelope.attention in {"reply", "alert"}
        or envelope.urgent
        or envelope.conversation_bound
        or envelope.source == "deploy-smoke"
    )


def _row_budget_exempt(row: sqlite3.Row | dict) -> bool:
    values = dict(row)
    try:
        metadata = json.loads(values.get("metadata") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        metadata = {}
    return bool(
        metadata.get("bypass_throttle")
        or values.get("kind") == "reply"
        or values.get("attention") in {"reply", "alert"}
        or metadata.get("urgent")
        or metadata.get("conversation_bound")
        or values.get("source") == "deploy-smoke"
    )


def _budgeted_deliveries(
    db: sqlite3.Connection,
    since: float,
) -> list[sqlite3.Row]:
    rows = db.execute(
        "SELECT source,kind,attention,metadata,delivered_epoch "
        "FROM delivery_envelopes WHERE delivered_epoch>=? "
        "ORDER BY delivered_epoch",
        (since,),
    ).fetchall()
    return [row for row in rows if not _row_budget_exempt(row)]


def _budgeted_reservations(
    db: sqlite3.Connection,
    since: float,
) -> list[sqlite3.Row]:
    rows = db.execute(
        "SELECT e.source,e.kind,e.attention,e.metadata,r.reserved_epoch "
        "FROM delivery_cap_reservations r "
        "JOIN delivery_envelopes e ON e.id=r.delivery_id "
        "WHERE r.reserved_epoch>=? ORDER BY r.reserved_epoch",
        (since,),
    ).fetchall()
    return [row for row in rows if not _row_budget_exempt(row)]


class DeliveryPipeline:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        db_path: str | Path | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.root = _root(root)
        self.path = _db_path(self.root, db_path)
        self.transport = transport or _default_transport(self.root)
        self.clock = clock
        self.sleeper = sleeper

    def _event(self, db: sqlite3.Connection, delivery_id: str,
               state: str, detail: str = "") -> None:
        db.execute(
            "INSERT INTO delivery_events "
            "(delivery_id,state,detail,created_epoch) VALUES (?,?,?,?)",
            (delivery_id, state, detail[:500], self.clock()),
        )

    def _set_state(self, db: sqlite3.Connection, delivery_id: str,
                   state: str, detail: str = "", **fields) -> None:
        if state not in DELIVERY_STATES:
            raise ValueError(f"invalid delivery state: {state}")
        unsupported = set(fields) - _STATE_UPDATE_FIELDS
        if unsupported:
            raise ValueError(
                "unsupported delivery fields: "
                + ", ".join(sorted(unsupported))
            )
        values = {"state": state, "updated_epoch": self.clock(), **fields}
        assignments = ", ".join(f"{key}=?" for key in values)
        db.execute(
            f"UPDATE delivery_envelopes SET {assignments} WHERE id=?",
            (*values.values(), delivery_id),
        )
        self._event(db, delivery_id, state, detail)

    def _insert(self, db: sqlite3.Connection, envelope: DeliveryEnvelope,
                content_hash: str, route: str, now: float) -> None:
        metadata = dict(envelope.metadata)
        metadata.update(
            urgent=bool(envelope.urgent),
            conversation_bound=bool(envelope.conversation_bound),
            chat_id=envelope.chat_id,
        )
        db.execute(
            """
            INSERT INTO delivery_envelopes (
                id,source,kind,attention,requested_channel,route_channel,state,
                content_hash,dedup_key,throttle_key,payload,metadata,provider,
                model,memorial_id,matter_id,reply_to,created_epoch,updated_epoch
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                envelope.id, envelope.source, envelope.kind, envelope.attention,
                envelope.requested_channel, route, "queued", content_hash,
                envelope.dedup_key, envelope.throttle_key,
                json.dumps(envelope.payload, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                envelope.provider, envelope.model, envelope.memorial_id,
                envelope.matter_id, envelope.reply_to, now, now,
            ),
        )
        self._event(db, envelope.id, "queued", "accepted")

    def _duplicate(self, db: sqlite3.Connection,
                   envelope: DeliveryEnvelope, content_hash: str,
                   now: float) -> sqlite3.Row | None:
        if envelope.dedup_key:
            row = db.execute(
                "SELECT * FROM delivery_envelopes WHERE dedup_key=? "
                "AND state IN ('queued','attempting','delivered','read','acted') "
                "ORDER BY created_epoch DESC LIMIT 1",
                (envelope.dedup_key,),
            ).fetchone()
            if row:
                return row
        return db.execute(
            "SELECT * FROM delivery_envelopes WHERE content_hash=? "
            "AND created_epoch>=? "
            "AND state IN ('queued','attempting','delivered','read','acted') "
            "ORDER BY created_epoch DESC LIMIT 1",
            (content_hash, now - DEDUP_WINDOW_SECONDS),
        ).fetchone()

    def _throttle_reason(self, db: sqlite3.Connection,
                         envelope: DeliveryEnvelope, now: float) -> str:
        if (envelope.metadata.get("bypass_throttle")
                or envelope.kind == "reply"
                or envelope.attention == "reply"):
            return ""
        local = datetime.fromtimestamp(now, tz=now_local().tzinfo)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        metric_key = envelope.throttle_key
        if metric_key:
            metric_cap = int(envelope.metadata.get("metric_daily_cap", 1) or 1)
            count = db.execute(
                "SELECT COUNT(*) FROM delivery_envelopes "
                "WHERE throttle_key=? AND created_epoch>=? "
                "AND state IN ('queued','attempting','delivered','read','acted')",
                (metric_key, start),
            ).fetchone()[0]
            # The current envelope is already inserted so that every policy
            # outcome has one durable row.  Exceeding, not merely reaching,
            # the cap suppresses it.
            if count > metric_cap:
                return "metric_daily_cap"
        source_cap = int(envelope.metadata.get(
            "source_daily_cap",
            os.environ.get("JARVIS_DELIVERY_SOURCE_DAILY_CAP",
                           DEFAULT_SOURCE_DAILY_CAP)))
        source_count = db.execute(
            "SELECT COUNT(*) FROM delivery_envelopes "
            "WHERE source=? AND created_epoch>=? "
            "AND state IN ('queued','attempting','delivered','read','acted')",
            (envelope.source, start),
        ).fetchone()[0]
        if source_count > source_cap:
            return "source_daily_cap"
        # The product-wide budget is a send-time capacity limit, not an
        # acceptance throttle. Overflow must remain queued for the next
        # window instead of becoming a permanent suppression.
        return ""

    def _reserve_attempt_cap(
        self,
        db: sqlite3.Connection,
        envelope: DeliveryEnvelope,
        now: float,
    ) -> tuple[str, float | None]:
        """Atomically reserve one send-day cap slot without holding network I/O.

        Creation-time throttling bounds accepted work, but a quiet-hours queue
        can cross midnight. Successful sends and live reservations are counted
        together under BEGIN IMMEDIATE, so concurrent workers cannot both take
        the final slot. The reservation is released after transport resolution.
        """
        if (envelope.metadata.get("bypass_throttle")
                or envelope.kind == "reply"
                or envelope.attention == "reply"):
            return "", None
        local = datetime.fromtimestamp(now, tz=now_local().tzinfo)
        start = local.replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM delivery_cap_reservations "
                "WHERE reserved_epoch<=?",
                (now - ATTEMPT_STALE_SECONDS,),
            )
            existing = db.execute(
                "SELECT 1 FROM delivery_cap_reservations WHERE delivery_id=?",
                (envelope.id,),
            ).fetchone()
            if existing:
                db.commit()
                return "", None

            reservations = (
                "SELECT COUNT(*) FROM delivery_cap_reservations "
                "WHERE day_start_epoch=?"
            )
            if envelope.throttle_key:
                metric_cap = int(
                    envelope.metadata.get("metric_daily_cap", 1) or 1)
                sent = db.execute(
                    "SELECT COUNT(*) FROM delivery_envelopes "
                    "WHERE throttle_key=? AND delivered_epoch>=?",
                    (envelope.throttle_key, start),
                ).fetchone()[0]
                held = db.execute(
                    reservations + " AND throttle_key=?",
                    (start, envelope.throttle_key),
                ).fetchone()[0]
                if sent + held >= metric_cap:
                    db.commit()
                    return "metric_daily_cap", None

            source_cap = int(envelope.metadata.get(
                "source_daily_cap",
                os.environ.get(
                    "JARVIS_DELIVERY_SOURCE_DAILY_CAP",
                    DEFAULT_SOURCE_DAILY_CAP,
                ),
            ))
            source_sent = db.execute(
                "SELECT COUNT(*) FROM delivery_envelopes "
                "WHERE source=? AND delivered_epoch>=?",
                (envelope.source, start),
            ).fetchone()[0]
            source_held = db.execute(
                reservations + " AND source=?",
                (start, envelope.source),
            ).fetchone()[0]
            if source_sent + source_held >= source_cap:
                db.commit()
                return "source_daily_cap", None

            if not _budget_exempt(envelope):
                global_cap = int(envelope.metadata.get(
                    "global_daily_cap",
                    os.environ.get(
                        "JARVIS_DELIVERY_GLOBAL_DAILY_CAP",
                        DEFAULT_GLOBAL_DAILY_CAP,
                    ),
                ))
                total_sent = len(_budgeted_deliveries(db, start))
                total_held = len(_budgeted_reservations(db, start))
                if total_sent + total_held >= global_cap:
                    db.commit()
                    moment = datetime.fromtimestamp(
                        now, tz=now_local().tzinfo)
                    return "global_daily_cap", _next_awake_epoch(moment)

                burst_window = max(1, int(envelope.metadata.get(
                    "burst_window_seconds",
                    os.environ.get(
                        "JARVIS_DELIVERY_BURST_WINDOW_SECONDS",
                        DEFAULT_BURST_WINDOW_SECONDS,
                    ),
                )))
                burst_cap = max(1, int(envelope.metadata.get(
                    "burst_cap",
                    os.environ.get(
                        "JARVIS_DELIVERY_BURST_CAP",
                        DEFAULT_BURST_CAP,
                    ),
                )))
                recent = _budgeted_deliveries(db, now - burst_window)
                recent_reservations = _budgeted_reservations(
                    db, now - burst_window)
                if len(recent) + len(recent_reservations) >= burst_cap:
                    retry_candidates = [
                        float(row["delivered_epoch"]) + burst_window
                        for row in recent
                    ]
                    retry_candidates.extend(
                        float(row["reserved_epoch"]) + burst_window
                        for row in recent_reservations
                    )
                    # A reservation may disappear as soon as its transport
                    # resolves. Recheck promptly instead of treating in-flight
                    # work as a completed send for the entire burst window.
                    if recent_reservations:
                        retry_candidates.append(
                            now + CAP_RESERVATION_RECHECK_SECONDS)
                    db.commit()
                    return "burst_budget", min(retry_candidates) + 0.001

            db.execute(
                "INSERT INTO delivery_cap_reservations "
                "(delivery_id,day_start_epoch,throttle_key,source,reserved_epoch) "
                "VALUES (?,?,?,?,?)",
                (
                    envelope.id,
                    start,
                    envelope.throttle_key,
                    envelope.source,
                    now,
                ),
            )
            db.commit()
            return "", None
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def _release_attempt_cap(
        db: sqlite3.Connection,
        delivery_id: str,
    ) -> None:
        db.execute(
            "DELETE FROM delivery_cap_reservations WHERE delivery_id=?",
            (delivery_id,),
        )

    def deliver(self, envelope: DeliveryEnvelope) -> DeliveryResult:
        envelope.normalized()
        if not envelope.provider or not envelope.model:
            provider, model = current_provider_model(self.root)
            envelope.provider = envelope.provider or provider
            envelope.model = envelope.model or model
        raw_content_hash = _content_hash(envelope)
        envelope, blocked = sanitize(envelope)
        now = self.clock()
        route = _route(envelope)
        content_hash = raw_content_hash if blocked else _content_hash(envelope)
        with closing(_connect(self.path)) as db, db:
            duplicate = (
                None if envelope.metadata.get("bypass_dedup")
                else self._duplicate(db, envelope, content_hash, now)
            )
            if duplicate:
                if (str(duplicate["state"]) == "queued"
                        and envelope.metadata.get("retry_existing")
                        and not envelope.metadata.get("force_queue")
                        and (
                            duplicate["next_attempt_epoch"] is None
                            or float(duplicate["next_attempt_epoch"]) <= now
                        )):
                    envelope.id = str(duplicate["id"])
                    return self._attempt(
                        envelope,
                        str(duplicate["route_channel"]) or route,
                    )
                return DeliveryResult(
                    str(duplicate["id"]), True, str(duplicate["state"]),
                    str(duplicate["route_channel"]),
                    str(duplicate["message_id"]), "duplicate",
                )
            self._insert(db, envelope, content_hash, route, now)
            if blocked:
                self._set_state(db, envelope.id, "suppressed", blocked,
                                last_error=blocked)
                return DeliveryResult(
                    envelope.id, True, "suppressed", route, reason=blocked)
            throttled = self._throttle_reason(db, envelope, now)
            if throttled:
                self._set_state(db, envelope.id, "suppressed", throttled,
                                last_error=throttled)
                return DeliveryResult(
                    envelope.id, True, "suppressed", route, reason=throttled)
            moment = datetime.fromtimestamp(now, tz=now_local().tzinfo)
            bypass_quiet = (
                envelope.urgent or envelope.conversation_bound
                or envelope.kind == "reply"
                or envelope.metadata.get("bypass_quiet")
            )
            if (envelope.metadata.get("force_queue")
                    or (not bypass_quiet and _quiet_now(moment))):
                next_attempt = _next_awake_epoch(moment)
                self._set_state(
                    db, envelope.id, "queued", "quiet_hours",
                    next_attempt_epoch=next_attempt,
                )
                return DeliveryResult(
                    envelope.id, True, "queued", route, reason="quiet_hours")
        return self._attempt(envelope, route)

    def _attempt(self, envelope: DeliveryEnvelope, route: str) -> DeliveryResult:
        claimed_at = self.clock()
        with closing(_connect(self.path)) as db:
            try:
                cursor = db.execute(
                    "UPDATE delivery_envelopes SET state='attempting',"
                    "updated_epoch=? WHERE id=? AND (state='queued' OR "
                    "(state='attempting' AND updated_epoch<=?))",
                    (
                        claimed_at, envelope.id,
                        claimed_at - ATTEMPT_STALE_SECONDS,
                    ),
                )
                row = db.execute(
                    "SELECT state,route_channel,message_id,last_error,attempts "
                    "FROM delivery_envelopes WHERE id=?",
                    (envelope.id,),
                ).fetchone()
                if cursor.rowcount != 1:
                    if not row:
                        return DeliveryResult(
                            envelope.id, False, "queued", route,
                            reason="delivery row disappeared")
                    state = str(row["state"])
                    return DeliveryResult(
                        envelope.id, True, state,
                        str(row["route_channel"] or route),
                        str(row["message_id"] or ""),
                        "in_progress" if state == "attempting"
                        else str(row["last_error"] or ""),
                    )
                prior_attempts = int(row["attempts"] or 0)
                self._event(
                    db, envelope.id, "attempting", "attempt claimed")
                db.commit()
            except Exception:
                db.rollback()
                raise

            throttled, retry_epoch = self._reserve_attempt_cap(
                db, envelope, claimed_at)
            if throttled:
                try:
                    deferred = throttled in {
                        "global_daily_cap", "burst_budget"}
                    self._set_state(
                        db,
                        envelope.id,
                        "queued" if deferred else "suppressed",
                        throttled,
                        next_attempt_epoch=(retry_epoch if deferred else None),
                        last_error=throttled,
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                return DeliveryResult(
                    envelope.id,
                    True,
                    "queued" if deferred else "suppressed",
                    route,
                    reason=throttled,
                )

            last_error = str(row["last_error"] or "")
            remaining = max(0, MAX_DELIVERY_ATTEMPTS - prior_attempts)
            delays = RETRY_DELAYS[:remaining]
            for offset, delay in enumerate(delays, 1):
                if delay:
                    self.sleeper(delay)
                number = prior_attempts + offset
                started = self.clock()
                try:
                    self._set_state(
                        db, envelope.id, "attempting",
                        f"attempt {number}", attempts=number,
                    )
                    cursor = db.execute(
                        "INSERT INTO delivery_attempts "
                        "(delivery_id,attempt,channel,started_epoch,status) "
                        "VALUES (?,?,?,?,?)",
                        (envelope.id, number, route, started, "attempting"),
                    )
                    attempt_id = cursor.lastrowid
                    db.commit()
                except Exception:
                    db.rollback()
                    raise

                try:
                    result = self.transport(envelope, route)
                    if not isinstance(result, TransportResult):
                        raise TypeError("transport must return TransportResult")
                except Exception as exc:
                    _ops_log(
                        "transport_exception",
                        level="error",
                        delivery_id=envelope.id,
                        route=route,
                        error_type=type(exc).__name__,
                    )
                    result = TransportResult(False, error=str(exc))

                try:
                    db.execute(
                        "UPDATE delivery_attempts SET finished_epoch=?,status=?,"
                        "error=?,message_id=? WHERE id=?",
                        (
                            self.clock(),
                            (
                                "delivered"
                                if result.ok
                                else "suppressed"
                                if result.suppressed_reason
                                else "failed"
                            ),
                            result.error[:500],
                            result.message_id,
                            attempt_id,
                        ),
                    )
                    if result.ok:
                        delivered_at = self.clock()
                        self._release_attempt_cap(db, envelope.id)
                        self._set_state(
                            db, envelope.id, "delivered",
                            "transport confirmed",
                            delivered_epoch=delivered_at,
                            next_attempt_epoch=None,
                            last_error="",
                            message_id=result.message_id,
                        )
                        try:
                            db.execute(
                                "INSERT INTO engagement_events "
                                "(event_type,source,timestamp,engaged,"
                                "gap_seconds,metadata) VALUES (?,?,?,?,?,?)",
                                (
                                    "sent", envelope.source,
                                    datetime.fromtimestamp(
                                        delivered_at,
                                        tz=now_local().tzinfo,
                                    ).isoformat(),
                                    0, 0,
                                    json.dumps({
                                        "delivery_id": envelope.id,
                                        "channel": route,
                                        "provider": envelope.provider,
                                        "model": envelope.model,
                                    }, ensure_ascii=False),
                                ),
                            )
                        except sqlite3.OperationalError:
                            # A standalone pipeline DB may intentionally contain
                            # only delivery tables.
                            pass
                        db.commit()
                        return DeliveryResult(
                            envelope.id, True, "delivered", route,
                            result.message_id)
                    if result.suppressed_reason:
                        self._release_attempt_cap(db, envelope.id)
                        self._set_state(
                            db,
                            envelope.id,
                            "suppressed",
                            result.suppressed_reason,
                            next_attempt_epoch=None,
                            last_error=result.suppressed_reason,
                        )
                        db.commit()
                        return DeliveryResult(
                            envelope.id,
                            True,
                            "suppressed",
                            route,
                            reason=result.suppressed_reason,
                        )
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                last_error = result.error or "transport failed"

            attempts = prior_attempts + len(delays)
            terminal = attempts >= MAX_DELIVERY_ATTEMPTS
            state = "failed" if terminal else "queued"
            next_attempt = None if terminal else self.clock() + 5 * 60
            try:
                self._release_attempt_cap(db, envelope.id)
                self._set_state(
                    db, envelope.id, state,
                    "retry terminal" if terminal else "retry batch exhausted",
                    next_attempt_epoch=next_attempt,
                    last_error=last_error[:500],
                )
                if (terminal
                        and not envelope.metadata.get("suppress_dead_letter")):
                    exists = db.execute(
                        "SELECT 1 FROM delivery_dead_letters "
                        "WHERE delivery_id=?",
                        (envelope.id,),
                    ).fetchone()
                    if not exists:
                        db.execute(
                            "INSERT INTO delivery_dead_letters "
                            "(delivery_id,source,kind,detail,created_epoch) "
                            "VALUES (?,?,?,?,?)",
                            (
                                envelope.id,
                                envelope.source,
                                envelope.kind,
                                last_error[:500],
                                self.clock(),
                            ),
                        )
                db.commit()
            except Exception:
                db.rollback()
                raise
            _ops_log(
                "terminal_failure" if terminal else "retry_batch_exhausted",
                level="error" if terminal else "warn",
                delivery_id=envelope.id,
                source=envelope.source,
                kind=envelope.kind,
                attempts=attempts,
                state=state,
            )
            return DeliveryResult(
                envelope.id, True, state, route, reason=last_error)

    def flush_due(self, limit: int = 50) -> list[DeliveryResult]:
        now = self.clock()
        with closing(_connect(self.path)) as db, db:
            rows = db.execute(
                "SELECT * FROM delivery_envelopes WHERE "
                "(state='queued' AND "
                "(next_attempt_epoch IS NULL OR next_attempt_epoch<=?)) "
                "OR (state='attempting' AND updated_epoch<=?) "
                "ORDER BY created_epoch LIMIT ?",
                (
                    now, now - ATTEMPT_STALE_SECONDS,
                    max(1, int(limit)),
                ),
            ).fetchall()
        results = []
        for row in rows:
            envelope = envelope_from_row(row)
            expires = float(envelope.metadata.get("expires_epoch") or 0)
            if expires and now > expires:
                with closing(_connect(self.path)) as db, db:
                    self._set_state(
                        db, envelope.id, "suppressed", "expired_ttl",
                        last_error="expired_ttl")
                results.append(DeliveryResult(
                    envelope.id, True, "suppressed",
                    str(row["route_channel"]), reason="expired_ttl"))
                continue
            if str(row["route_channel"]) in {"web", "push"}:
                # Legacy rows queued for the retired web surface (REQ-119) or
                # the retired phone push surface (REQ-120): attempting them
                # would only churn retries into dead letters, and marking them
                # delivered would resurrect the fake ledger.
                reason = f"{row['route_channel']}_surface_retired"
                with closing(_connect(self.path)) as db, db:
                    self._set_state(
                        db, envelope.id, "suppressed", reason,
                        last_error=reason)
                results.append(DeliveryResult(
                    envelope.id, True, "suppressed",
                    str(row["route_channel"]), reason=reason))
                continue
            moment = datetime.fromtimestamp(now, tz=now_local().tzinfo)
            if (_quiet_now(moment) and not envelope.urgent
                    and not envelope.conversation_bound
                    and envelope.kind != "reply"):
                continue
            results.append(self._attempt(
                envelope, str(row["route_channel"]) or _route(envelope)))
        return results

    def suppress_queued_source(
        self, source: str, *, reason: str,
    ) -> list[str]:
        """Suppress every currently queued envelope from one retired path.

        This is an explicit reconciliation primitive for product migrations;
        it never touches attempting or delivered work and records one normal
        delivery event per row so the queue cannot diverge from its history.
        """
        source = str(source or "").strip()
        reason = str(reason or "").strip()
        if not source:
            raise ValueError("source is required")
        if not reason:
            raise ValueError("reason is required")
        with closing(_connect(self.path)) as db, db:
            rows = db.execute(
                "SELECT id FROM delivery_envelopes "
                "WHERE source=? AND state='queued' ORDER BY created_epoch",
                (source,),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            for delivery_id in ids:
                self._set_state(
                    db, delivery_id, "suppressed", reason,
                    next_attempt_epoch=None, last_error=reason,
                )
        return ids

    def list_source(
        self, source: str, *, state: str = "", last_error: str = "",
    ) -> list[dict]:
        """Return all envelopes for an exact source and optional state/reason.

        Unlike the operator-facing ``list`` method this is intentionally not
        capped: reconciliation must repair every row from a prior partial run,
        not just the newest dashboard page.
        """
        source = str(source or "").strip()
        if not source:
            raise ValueError("source is required")
        clauses = ["source=?"]
        params: list = [source]
        if state:
            clauses.append("state=?")
            params.append(str(state))
        if last_error:
            clauses.append("last_error=?")
            params.append(str(last_error))
        query = (
            "SELECT * FROM delivery_envelopes WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_epoch"
        )
        with closing(_connect(self.path)) as db, db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def confirm(self, delivery_id: str, state: str) -> DeliveryResult:
        if state not in {"read", "acted"}:
            raise ValueError("confirmation state must be read or acted")
        now = self.clock()
        with closing(_connect(self.path)) as db, db:
            row = db.execute(
                "SELECT * FROM delivery_envelopes WHERE id=?",
                (delivery_id,),
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            current = str(row["state"])
            if current not in FINAL_SUCCESS_STATES:
                raise ValueError(
                    f"cannot mark {current} delivery as {state}")
            if current == "acted":
                state = "acted"
            elif current == "read" and state == "read":
                state = "read"
            fields = (
                {"acted_epoch": now}
                if state == "acted" else {"read_epoch": now}
            )
            self._set_state(db, delivery_id, state, "user confirmation",
                            **fields)
            updated = db.execute(
                "SELECT * FROM delivery_envelopes WHERE id=?",
                (delivery_id,),
            ).fetchone()
        return DeliveryResult(
            delivery_id, True, state, str(updated["route_channel"]),
            str(updated["message_id"]))

    def confirm_entity(self, *, memorial_id: str = "", message_id: str = "",
                       state: str = "read") -> list[DeliveryResult]:
        """Confirm every delivered row associated with one user-visible entity."""
        if not memorial_id and not message_id:
            return []
        where, value = (
            ("memorial_id", memorial_id) if memorial_id
            else ("message_id", message_id)
        )
        with closing(_connect(self.path)) as db, db:
            rows = db.execute(
                f"SELECT id FROM delivery_envelopes WHERE {where}=? "
                "AND state IN ('delivered','read','acted')",
                (value,),
            ).fetchall()
        results = []
        for row in rows:
            try:
                results.append(self.confirm(str(row["id"]), state))
            except (KeyError, ValueError):
                continue
        return results

    def get(self, delivery_id: str) -> dict | None:
        with closing(_connect(self.path)) as db, db:
            row = db.execute(
                "SELECT * FROM delivery_envelopes WHERE id=?",
                (delivery_id,),
            ).fetchone()
        return dict(row) if row else None

    def list(self, *, state: str = "", limit: int = 100) -> list[dict]:
        query = "SELECT * FROM delivery_envelopes"
        params: list = []
        if state:
            query += " WHERE state=?"
            params.append(state)
        query += " ORDER BY created_epoch DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with closing(_connect(self.path)) as db, db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def pending_dead_letters(self, limit: int = 100) -> list[dict]:
        with closing(_connect(self.path)) as db, db:
            rows = db.execute(
                "SELECT * FROM delivery_dead_letters "
                "WHERE notified_epoch IS NULL ORDER BY created_epoch LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_dead_letters_notified(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with closing(_connect(self.path)) as db, db:
            db.execute(
                f"UPDATE delivery_dead_letters SET notified_epoch=? "
                f"WHERE id IN ({placeholders})",
                (self.clock(), *ids),
            )


def envelope_from_row(row: sqlite3.Row | dict) -> DeliveryEnvelope:
    values = dict(row)
    try:
        payload = json.loads(values.get("payload") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = {}
    try:
        metadata = json.loads(values.get("metadata") or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        metadata = {}
    return DeliveryEnvelope(
        id=str(values.get("id") or ""),
        source=str(values.get("source") or "unknown"),
        kind=str(values.get("kind") or "text"),
        payload=payload,
        attention=str(values.get("attention") or "notice"),
        requested_channel=str(values.get("requested_channel") or "auto"),
        urgent=bool(metadata.get("urgent")),
        conversation_bound=bool(metadata.get("conversation_bound")),
        reply_to=str(values.get("reply_to") or ""),
        chat_id=str(metadata.get("chat_id") or ""),
        memorial_id=str(values.get("memorial_id") or ""),
        matter_id=str(values.get("matter_id") or ""),
        dedup_key=str(values.get("dedup_key") or ""),
        throttle_key=str(values.get("throttle_key") or ""),
        provider=str(values.get("provider") or ""),
        model=str(values.get("model") or ""),
        metadata=metadata,
    )


def current_provider_model(root: str | Path | None = None) -> tuple[str, str]:
    """Best available provider/model label for envelope forensics."""
    provider = (os.environ.get("JARVIS_DELIVERY_PROVIDER")
                or os.environ.get("JV_PROVIDER") or "").strip()
    model = (os.environ.get("JARVIS_DELIVERY_MODEL")
             or os.environ.get("JV_MODEL") or "").strip()
    if provider and model:
        return provider, model
    state = {}
    try:
        state = json.loads(
            (_root(root) / "data" / "provider_state.json").read_text(
                encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    if not provider:
        provider = "Claude backup" if state.get("spend_limit_since") else "Claude primary"
    if not model:
        if state.get("spend_limit_since"):
            model = os.environ.get("CLAUDE_BACKUP_MODEL", "")
        else:
            model = os.environ.get("MAIN_MODEL", "")
    return provider, model


def deliver(envelope: DeliveryEnvelope, *,
            root: str | Path | None = None,
            db_path: str | Path | None = None,
            transport: Transport | None = None) -> DeliveryResult:
    return DeliveryPipeline(
        root, db_path=db_path, transport=transport).deliver(envelope)


def flush_due(root: str | Path | None = None, limit: int = 50) -> list[DeliveryResult]:
    return DeliveryPipeline(root).flush_due(limit)


def _cli_send(args: argparse.Namespace) -> int:
    text = sys.stdin.read() if args.stdin else args.text
    kind = "reply" if args.reply_to else args.kind
    payload = (
        {"card_json": text, "text": extract_card_text(text)}
        if kind == "card"
        else {"text": text}
    )
    envelope = DeliveryEnvelope(
        source=args.source,
        kind=kind,
        payload=payload,
        attention="reply" if args.reply_to else args.attention,
        requested_channel=args.channel,
        urgent=args.urgent,
        conversation_bound=bool(args.reply_to),
        reply_to=args.reply_to,
        provider=args.provider,
        model=args.model,
        metadata={"bypass_throttle": bool(args.reply_to)},
    )
    result = deliver(envelope)
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.accepted else 1


def _cli_confirm_read(args: argparse.Namespace) -> int:
    values = list(args.message_id or [])
    if args.stdin:
        try:
            incoming = json.loads(sys.stdin.read() or "[]")
        except json.JSONDecodeError:
            incoming = []
        if isinstance(incoming, list):
            values.extend(str(value) for value in incoming)
    pipeline = DeliveryPipeline()
    confirmed = []
    for message_id in dict.fromkeys(value for value in values if value):
        confirmed.extend(
            asdict(result)
            for result in pipeline.confirm_entity(
                message_id=message_id, state="read")
        )
    print(json.dumps(confirmed, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis unified delivery pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send")
    send.add_argument("--source", required=True)
    send.add_argument(
        # No "web" (REQ-119) and no "push" (REQ-120): both surfaces are
        # retired and the kinds are rejected by DeliveryEnvelope.normalized()
        # — don't offer them here.
        "--kind", default="text", choices=["text", "card"]
    )
    send.add_argument("--attention", default="notice",
                      choices=["decision", "notice", "alert", "reply"])
    send.add_argument("--channel", default="auto")
    send.add_argument("--reply-to", default="")
    send.add_argument("--text", default="")
    send.add_argument("--stdin", action="store_true")
    send.add_argument("--urgent", action="store_true")
    send.add_argument("--provider", default="")
    send.add_argument("--model", default="")
    send.set_defaults(func=_cli_send)

    flush = sub.add_parser("flush")
    flush.add_argument("--limit", type=int, default=50)
    flush.set_defaults(func=lambda a: (
        print(json.dumps([asdict(r) for r in flush_due(limit=a.limit)],
                         ensure_ascii=False)) or 0))

    status = sub.add_parser("status")
    status.add_argument("--state", default="")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(func=lambda a: (
        print(json.dumps(DeliveryPipeline().list(
            state=a.state, limit=a.limit), ensure_ascii=False, indent=2)) or 0))

    confirm_read = sub.add_parser("confirm-read")
    confirm_read.add_argument("--message-id", action="append", default=[])
    confirm_read.add_argument("--stdin", action="store_true")
    confirm_read.set_defaults(func=_cli_confirm_read)

    parsed = parser.parse_args(argv)
    return int(parsed.func(parsed))


if __name__ == "__main__":
    raise SystemExit(main())
