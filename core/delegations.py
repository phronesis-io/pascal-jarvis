"""Verified delegation control plane.

A Delegation is an accepted responsibility with a machine-checkable outcome
contract.  Workers may execute steps and report observations, but only this
module's deterministic evaluator can mark the responsibility complete.

The store deliberately keeps summaries, stable object identifiers, digests,
and artifact locators.  It does not persist private message bodies, tokens, or
model reasoning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


ACTIVE_STATUSES = {
    "captured",
    "needs_clarification",
    "bound",
    "executing",
    "verifying",
    "awaiting_external",
    "needs_user",
    "blocked",
    "failed",
}
TERMINAL_STATUSES = {"completed", "cancelled", "superseded"}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
STEP_STATUSES = {
    "pending",
    "blocked",
    "executing",
    "verifying",
    "awaiting_external",
    "completed",
    "failed",
    "cancelled",
    "superseded",
}
QUALIFYING_STRENGTHS = {"strong", "corroborated", "user_attested"}
PRIVACY_CLASSES = {"public", "internal", "private", "restricted"}
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,500}$")


class DelegationError(RuntimeError):
    """Base error for an invalid control-plane operation."""


class DelegationConflict(DelegationError):
    """The caller operated on a stale contract version or active lease."""


class DelegationNotFound(DelegationError):
    """The requested delegation or step does not exist."""


def is_confirmable(delegation: dict[str, Any] | sqlite3.Row) -> bool:
    """Return whether the owner can grant the contract's pending R3 approval."""
    values = dict(delegation)
    policy = values.get("verification_policy")
    if policy is None:
        try:
            policy = json.loads(
                str(values.get("verification_policy_json") or "{}")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            policy = {}
    return bool(
        str(values.get("status") or "") == "needs_user"
        and int(values.get("risk_tier") or 0) == 3
        and not bool(values.get("authorized"))
        and str(values.get("waiting_on") or "") in {"", "user"}
        and str(values.get("target_id") or "")
        and str(values.get("authority") or "")
        and isinstance(policy, dict)
        and bool(policy)
    )


def is_retryable(delegation: dict[str, Any] | sqlite3.Row) -> bool:
    """Return whether retry is a valid recovery transition for this contract."""
    values = dict(delegation)
    status = str(values.get("status") or "")
    return bool(
        status in {"failed", "blocked", "verifying"}
        or (
            status == "needs_user"
            and str(values.get("waiting_on") or "") == "verification_recovery"
            and int(values.get("risk_tier") or 0) < 4
            and bool(values.get("authorized"))
        )
    )


@dataclass(frozen=True, slots=True)
class Claim:
    delegation_id: str
    step_id: str
    contract_version: int
    lease_owner: str
    lease_expires_at: float


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: Any, field: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise DelegationError(f"{field} must be an object")
    encoded = _json(value)
    if len(encoded) > 20_000:
        raise DelegationError(f"{field} exceeds 20KB")
    lowered = encoded.casefold()
    for marker in (
        "authorization:",
        "bearer ",
        "access_token",
        "refresh_token",
        "api_key",
        "private_key",
        "cookie",
    ):
        if marker in lowered:
            raise DelegationError(f"{field} appears to contain a secret")
    return value


def _text(value: Any, field: str, *, limit: int = 1000, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise DelegationError(f"{field} is required")
    if len(result) > limit:
        raise DelegationError(f"{field} exceeds {limit} characters")
    return result


def _safe_ref(value: Any, field: str, *, required: bool = False) -> str:
    result = _text(value, field, limit=500, required=required)
    if result and not SAFE_KEY_RE.fullmatch(result):
        raise DelegationError(f"{field} contains unsafe characters")
    return result


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class DelegationStore:
    """SQLite-backed state machine and completion evaluator."""

    def __init__(
        self,
        root: str | Path | None = None,
        db_path: str | Path | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.root = Path(
            root
            or os.environ.get("JARVIS_DIR")
            or Path(__file__).resolve().parent.parent
        )
        self.db_path = Path(
            db_path
            or os.environ.get("JARVIS_DB_PATH")
            or self.root / "data" / "jarvis.db"
        )
        self.now = now
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.db_path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS delegations (
                    id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    capture_mode TEXT NOT NULL DEFAULT 'explicit',
                    title TEXT NOT NULL,
                    request_summary TEXT NOT NULL DEFAULT '',
                    matter_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    risk_tier INTEGER NOT NULL DEFAULT 0,
                    authorized INTEGER NOT NULL DEFAULT 0,
                    contract_version INTEGER NOT NULL DEFAULT 1,
                    operation TEXT NOT NULL,
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    target_label TEXT NOT NULL DEFAULT '',
                    expected_postcondition_json TEXT NOT NULL DEFAULT '{}',
                    authority TEXT NOT NULL DEFAULT '',
                    verification_policy_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT NOT NULL,
                    waiting_on TEXT NOT NULL DEFAULT '',
                    deadline_at REAL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    verified_at REAL,
                    completed_at REAL,
                    updated_at REAL NOT NULL,
                    privacy_class TEXT NOT NULL DEFAULT 'private',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error_summary TEXT NOT NULL DEFAULT '',
                    UNIQUE(principal_id, source, source_ref),
                    UNIQUE(idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_delegations_active
                    ON delegations(status, deadline_at, updated_at);
                CREATE INDEX IF NOT EXISTS idx_delegations_matter
                    ON delegations(matter_id, updated_at);

                CREATE TABLE IF NOT EXISTS delegation_steps (
                    id TEXT PRIMARY KEY,
                    delegation_id TEXT NOT NULL,
                    contract_version INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    executor TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    required INTEGER NOT NULL DEFAULT 1,
                    idempotency_key TEXT NOT NULL,
                    depends_on_json TEXT NOT NULL DEFAULT '[]',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    artifact_locator TEXT NOT NULL DEFAULT '',
                    started_at REAL,
                    finished_at REAL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(delegation_id) REFERENCES delegations(id),
                    UNIQUE(delegation_id, contract_version, sequence),
                    UNIQUE(idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_delegation_steps_claim
                    ON delegation_steps(status, lease_expires_at, sequence);

                CREATE TABLE IF NOT EXISTS delegation_evidence (
                    id TEXT PRIMARY KEY,
                    delegation_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    contract_version INTEGER NOT NULL,
                    evidence_type TEXT NOT NULL,
                    strength TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    resource_locator TEXT NOT NULL DEFAULT '',
                    observed_digest TEXT NOT NULL,
                    expected_summary TEXT NOT NULL DEFAULT '',
                    observed_summary TEXT NOT NULL DEFAULT '',
                    matched INTEGER NOT NULL,
                    observed_at REAL NOT NULL,
                    expires_at REAL,
                    privacy_class TEXT NOT NULL DEFAULT 'private',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(delegation_id) REFERENCES delegations(id),
                    FOREIGN KEY(step_id) REFERENCES delegation_steps(id)
                );
                CREATE INDEX IF NOT EXISTS idx_delegation_evidence_eval
                    ON delegation_evidence(delegation_id, contract_version,
                                           step_id, matched, strength);

                CREATE TABLE IF NOT EXISTS delegation_events (
                    event_id TEXT PRIMARY KEY,
                    delegation_id TEXT NOT NULL,
                    contract_version INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL DEFAULT '',
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    reason_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(delegation_id) REFERENCES delegations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_delegation_events_timeline
                    ON delegation_events(delegation_id, created_at);

                CREATE TABLE IF NOT EXISTS delegation_links (
                    delegation_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'projects',
                    created_at REAL NOT NULL,
                    PRIMARY KEY(delegation_id, entity_type, entity_id, relation),
                    FOREIGN KEY(delegation_id) REFERENCES delegations(id)
                );

                CREATE TABLE IF NOT EXISTS delegation_shadow_labels (
                    delegation_id TEXT PRIMARY KEY,
                    predicted_is_delegation INTEGER NOT NULL,
                    predicted_target_risk INTEGER NOT NULL DEFAULT 0,
                    predicted_verifier TEXT NOT NULL DEFAULT '',
                    actual_is_delegation INTEGER,
                    actual_target_risk INTEGER,
                    actual_verifier TEXT,
                    labeled_at REAL,
                    FOREIGN KEY(delegation_id) REFERENCES delegations(id)
                );
                """
            )

    @contextmanager
    def _tx(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _event(
        self,
        db: sqlite3.Connection,
        delegation_id: str,
        version: int,
        event_type: str,
        *,
        actor_type: str = "system",
        actor_id: str = "",
        from_status: str = "",
        to_status: str = "",
        reason_code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO delegation_events(
                event_id,delegation_id,contract_version,event_type,actor_type,
                actor_id,from_status,to_status,reason_code,created_at,metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _new_id("dle"),
                delegation_id,
                version,
                event_type,
                actor_type,
                _text(actor_id, "actor_id", limit=200),
                from_status,
                to_status,
                reason_code,
                self.now(),
                _json(_object(metadata or {}, "event metadata")),
            ),
        )

    @staticmethod
    def _action_key(
        principal_id: str,
        operation: str,
        target_id: str,
        expected: dict[str, Any],
        version: int,
        source_ref: str,
    ) -> str:
        material = "\0".join(
            (
                principal_id,
                operation,
                target_id,
                _json(expected),
                str(version),
                source_ref,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def create(
        self,
        *,
        principal_id: str,
        source: str,
        source_ref: str,
        title: str,
        operation: str,
        request_summary: str = "",
        matter_id: str = "",
        risk_tier: int = 0,
        target_type: str = "",
        target_id: str = "",
        target_label: str = "",
        expected_postcondition: dict[str, Any] | None = None,
        authority: str = "",
        verification_policy: dict[str, Any] | None = None,
        deadline_at: float | None = None,
        privacy_class: str = "private",
        capture_mode: str = "explicit",
        authorized: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Create once for a stable source event; returns ``(row, created)``."""
        principal_id = _safe_ref(principal_id, "principal_id", required=True)
        source = _safe_ref(source, "source", required=True)
        source_ref = _safe_ref(source_ref, "source_ref", required=True)
        title = _text(title, "title", limit=300, required=True)
        operation = _safe_ref(operation, "operation", required=True)
        request_summary = _text(request_summary, "request_summary", limit=1000)
        matter_id = _safe_ref(matter_id, "matter_id")
        target_type = _safe_ref(target_type, "target_type")
        target_id = _safe_ref(target_id, "target_id")
        target_label = _text(target_label, "target_label", limit=300)
        authority = _safe_ref(authority, "authority")
        expected = _object(expected_postcondition or {}, "expected_postcondition")
        policy = _object(verification_policy or {}, "verification_policy")
        if privacy_class not in PRIVACY_CLASSES:
            raise DelegationError("invalid privacy_class")
        if capture_mode not in {"explicit", "authorized_rule", "shadow"}:
            raise DelegationError("invalid capture_mode")
        risk_tier = int(risk_tier)
        if risk_tier < 0 or risk_tier > 4:
            raise DelegationError("risk_tier must be in R0-R4")
        if deadline_at is not None:
            deadline_at = float(deadline_at)

        status = "captured"
        if target_id and authority and policy:
            status = "bound"
        if risk_tier == 4 or (risk_tier >= 3 and not authorized):
            status = "needs_user"
        if risk_tier == 4:
            authorized = False
        if capture_mode == "shadow":
            status = "captured"
        now = self.now()
        delegation_id = _new_id("dlg")
        key = self._action_key(
            principal_id, operation, target_id, expected, 1, source_ref
        )
        # A normal idempotent replay also repairs any projection that was
        # interrupted after the authoritative row committed.
        with closing(self._connect()) as read_db:
            existing = read_db.execute(
                """
                SELECT * FROM delegations
                 WHERE principal_id=? AND source=? AND source_ref=?
                """,
                (principal_id, source, source_ref),
            ).fetchone()
        if existing is not None:
            result = _row(existing) or {}
            self.sync_projection(str(result["id"]))
            return result, False
        with self._tx() as db:
            existing = db.execute(
                """
                SELECT * FROM delegations
                 WHERE principal_id=? AND source=? AND source_ref=?
                """,
                (principal_id, source, source_ref),
            ).fetchone()
            if existing is not None:
                return _row(existing) or {}, False
            db.execute(
                """
                INSERT INTO delegations(
                    id,principal_id,source,source_ref,capture_mode,title,
                    request_summary,matter_id,status,risk_tier,authorized,
                    contract_version,operation,target_type,target_id,target_label,
                    expected_postcondition_json,authority,
                    verification_policy_json,idempotency_key,deadline_at,
                    created_at,updated_at,privacy_class
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    delegation_id,
                    principal_id,
                    source,
                    source_ref,
                    capture_mode,
                    title,
                    request_summary,
                    matter_id,
                    status,
                    risk_tier,
                    int(authorized),
                    1,
                    operation,
                    target_type,
                    target_id,
                    target_label,
                    _json(expected),
                    authority,
                    _json(policy),
                    key,
                    deadline_at,
                    now,
                    now,
                    privacy_class,
                ),
            )
            self._event(
                db,
                delegation_id,
                1,
                "delegation.captured",
                actor_type="principal" if capture_mode == "explicit" else "system",
                actor_id=principal_id,
                to_status=status,
                metadata={"capture_mode": capture_mode},
            )
            if status == "bound":
                self._event(
                    db,
                    delegation_id,
                    1,
                    "delegation.bound",
                    to_status="bound",
                )
            elif status == "needs_user":
                self._event(
                    db,
                    delegation_id,
                    1,
                    "delegation.confirmation_required",
                    to_status="needs_user",
                    reason_code="risk_confirmation",
                )
            created = db.execute(
                "SELECT * FROM delegations WHERE id=?", (delegation_id,)
            ).fetchone()
        result = _row(created) or {}
        self.sync_projection(delegation_id)
        return result, True

    def record_shadow_prediction(
        self,
        *,
        principal_id: str,
        source: str,
        source_ref: str,
        title: str,
        operation: str,
        predicted_is_delegation: bool,
        predicted_target_risk: int,
        predicted_verifier: str,
        matter_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Persist a Phase-0 prediction without creating user-visible work."""
        predicted_target_risk = int(predicted_target_risk)
        if predicted_target_risk < 0 or predicted_target_risk > 4:
            raise DelegationError("predicted_target_risk must be in R0-R4")
        predicted_verifier = _safe_ref(
            predicted_verifier, "predicted_verifier"
        )
        delegation, created = self.create(
            principal_id=principal_id,
            source=source,
            source_ref=source_ref,
            title=title,
            operation=operation,
            request_summary="",
            matter_id=matter_id,
            risk_tier=predicted_target_risk,
            verification_policy={"verifier": predicted_verifier},
            privacy_class="private",
            capture_mode="shadow",
        )
        with self._tx() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO delegation_shadow_labels(
                    delegation_id,predicted_is_delegation,
                    predicted_target_risk,predicted_verifier
                ) VALUES (?,?,?,?)
                """,
                (
                    delegation["id"],
                    int(predicted_is_delegation),
                    predicted_target_risk,
                    predicted_verifier,
                ),
            )
        return delegation, created

    def bind(
        self,
        delegation_id: str,
        *,
        expected_version: int,
        target_type: str,
        target_id: str,
        target_label: str,
        expected_postcondition: dict[str, Any],
        authority: str,
        verification_policy: dict[str, Any],
        authorized: bool = False,
        actor_id: str = "",
    ) -> dict[str, Any]:
        target_type = _safe_ref(target_type, "target_type", required=True)
        target_id = _safe_ref(target_id, "target_id", required=True)
        target_label = _text(target_label, "target_label", limit=300)
        authority = _safe_ref(authority, "authority", required=True)
        expected = _object(expected_postcondition, "expected_postcondition")
        policy = _object(verification_policy, "verification_policy")
        now = self.now()
        with self._tx() as db:
            current = self._require(db, delegation_id)
            self._version(current, expected_version)
            initial_unbound_risk_review = bool(
                current["status"] == "needs_user"
                and not current["target_id"]
                and not current["authority"]
            )
            if (
                current["status"] not in {"captured", "needs_clarification"}
                and not initial_unbound_risk_review
            ):
                raise DelegationConflict(
                    "binding is only allowed before execution; "
                    "revise the contract to change a bound target"
                )
            risk_tier = int(current["risk_tier"])
            approved = bool(authorized or current["authorized"])
            status = (
                "needs_user"
                if risk_tier == 4 or (risk_tier >= 3 and not approved)
                else "bound"
            )
            if risk_tier == 4:
                approved = False
            key = self._action_key(
                current["principal_id"],
                current["operation"],
                target_id,
                expected,
                expected_version,
                current["source_ref"],
            )
            db.execute(
                """
                UPDATE delegations
                   SET target_type=?,target_id=?,target_label=?,
                       expected_postcondition_json=?,authority=?,
                       verification_policy_json=?,idempotency_key=?,
                       authorized=?,status=?,waiting_on=?,updated_at=?
                 WHERE id=?
                """,
                (
                    target_type,
                    target_id,
                    target_label,
                    _json(expected),
                    authority,
                    _json(policy),
                    key,
                    int(approved),
                    status,
                    "user" if status == "needs_user" else "",
                    now,
                    delegation_id,
                ),
            )
            self._event(
                db,
                delegation_id,
                expected_version,
                (
                    "delegation.confirmation_required"
                    if status == "needs_user"
                    else "delegation.bound"
                ),
                actor_type="principal" if authorized else "system",
                actor_id=actor_id,
                from_status=current["status"],
                to_status=status,
            )
            result = self._require(db, delegation_id)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def revise_contract(
        self,
        delegation_id: str,
        *,
        expected_version: int,
        target_id: str | None = None,
        expected_postcondition: dict[str, Any] | None = None,
        verification_policy: dict[str, Any] | None = None,
        actor_id: str = "",
    ) -> dict[str, Any]:
        """Create a new contract version; old evidence remains non-qualifying."""
        now = self.now()
        with self._tx() as db:
            current = self._require(db, delegation_id)
            self._version(current, expected_version)
            if current["status"] in TERMINAL_STATUSES:
                raise DelegationConflict("terminal delegation cannot be revised")
            new_version = expected_version + 1
            new_target = _safe_ref(
                target_id if target_id is not None else current["target_id"],
                "target_id",
            )
            expected = (
                _object(expected_postcondition, "expected_postcondition")
                if expected_postcondition is not None
                else json.loads(current["expected_postcondition_json"])
            )
            policy = (
                _object(verification_policy, "verification_policy")
                if verification_policy is not None
                else json.loads(current["verification_policy_json"])
            )
            key = self._action_key(
                current["principal_id"],
                current["operation"],
                new_target,
                expected,
                new_version,
                current["source_ref"],
            )
            db.execute(
                """
                UPDATE delegation_steps
                   SET status='superseded',lease_owner='',lease_expires_at=NULL,
                       finished_at=?,updated_at=?
                 WHERE delegation_id=? AND contract_version=?
                   AND status NOT IN ('completed','cancelled','superseded')
                """,
                (now, now, delegation_id, expected_version),
            )
            status = (
                "needs_user" if int(current["risk_tier"]) >= 3 else "bound"
            )
            db.execute(
                """
                UPDATE delegations
                   SET contract_version=?,target_id=?,
                       expected_postcondition_json=?,verification_policy_json=?,
                       idempotency_key=?,
                       status=?,authorized=0,waiting_on='',completed_at=NULL,
                       verified_at=NULL,updated_at=?
                 WHERE id=?
                """,
                (
                    new_version,
                    new_target,
                    _json(expected),
                    _json(policy),
                    key,
                    status,
                    now,
                    delegation_id,
                ),
            )
            self._event(
                db,
                delegation_id,
                new_version,
                "delegation.contract_revised",
                actor_type="principal",
                actor_id=actor_id,
                from_status=current["status"],
                to_status=status,
                metadata={"previous_version": expected_version},
            )
            result = self._require(db, delegation_id)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def add_step(
        self,
        delegation_id: str,
        *,
        expected_version: int,
        sequence: int,
        kind: str,
        executor: str = "",
        required: bool = True,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        kind = _safe_ref(kind, "kind", required=True)
        executor = _safe_ref(executor, "executor")
        dependencies = [
            _safe_ref(value, "depends_on") for value in (depends_on or [])
        ]
        now = self.now()
        step_id = _new_id("dls")
        with self._tx() as db:
            current = self._require(db, delegation_id)
            self._version(current, expected_version)
            if current["status"] in TERMINAL_STATUSES:
                raise DelegationConflict("cannot add a step to a terminal delegation")
            if current["capture_mode"] == "shadow":
                raise DelegationConflict("shadow delegation cannot have executable steps")
            key = hashlib.sha256(
                f"{current['idempotency_key']}\0{sequence}\0{kind}".encode("utf-8")
            ).hexdigest()
            db.execute(
                """
                INSERT INTO delegation_steps(
                    id,delegation_id,contract_version,sequence,kind,executor,
                    status,required,idempotency_key,depends_on_json,updated_at
                ) VALUES (?,?,?,?,?,?,'pending',?,?,?,?)
                """,
                (
                    step_id,
                    delegation_id,
                    expected_version,
                    int(sequence),
                    kind,
                    executor,
                    int(required),
                    key,
                    _json(dependencies),
                    now,
                ),
            )
            self._event(
                db,
                delegation_id,
                expected_version,
                "delegation.step_added",
                metadata={"step_id": step_id, "kind": kind, "sequence": int(sequence)},
            )
            result = db.execute(
                "SELECT * FROM delegation_steps WHERE id=?", (step_id,)
            ).fetchone()
        return _row(result) or {}

    def claim_step(
        self,
        delegation_id: str,
        step_id: str,
        *,
        expected_version: int,
        owner: str,
        lease_seconds: int = 900,
    ) -> Claim:
        owner = _safe_ref(owner, "owner", required=True)
        lease_seconds = max(30, min(int(lease_seconds), 7200))
        now = self.now()
        with self._tx() as db:
            delegation = self._require(db, delegation_id)
            self._version(delegation, expected_version)
            if delegation["status"] in TERMINAL_STATUSES | {
                "captured",
                "needs_clarification",
                "needs_user",
            }:
                raise DelegationConflict(
                    f"delegation status {delegation['status']} is not executable"
                )
            if delegation["capture_mode"] == "shadow":
                raise DelegationConflict("shadow delegation is not executable")
            if int(delegation["risk_tier"]) == 4:
                raise DelegationConflict("R4 delegation must remain human-operated")
            if int(delegation["risk_tier"]) >= 3 and not delegation["authorized"]:
                raise DelegationConflict("delegation requires principal approval")
            if (
                not delegation["target_id"]
                or not delegation["authority"]
                or not json.loads(delegation["verification_policy_json"] or "{}")
            ):
                raise DelegationConflict("delegation contract is not bound")
            step = self._require_step(db, delegation_id, step_id, expected_version)
            if step["status"] == "completed":
                raise DelegationConflict("step is already complete")
            if step["status"] not in {"pending", "blocked", "executing"}:
                raise DelegationConflict(f"step status {step['status']} is not claimable")
            lease_expiry = float(step["lease_expires_at"] or 0)
            if step["status"] == "executing" and lease_expiry > now:
                raise DelegationConflict("step has an active lease")
            dependencies = json.loads(step["depends_on_json"] or "[]")
            if dependencies:
                rows = db.execute(
                    """
                    SELECT id,status FROM delegation_steps
                     WHERE delegation_id=? AND contract_version=?
                    """,
                    (delegation_id, expected_version),
                ).fetchall()
                states = {row["id"]: row["status"] for row in rows}
                missing = [
                    dependency
                    for dependency in dependencies
                    if states.get(dependency) != "completed"
                ]
                if missing:
                    raise DelegationConflict(
                        f"step dependencies are incomplete: {','.join(missing)}"
                    )
            expires = now + lease_seconds
            db.execute(
                """
                UPDATE delegation_steps
                   SET status='executing',lease_owner=?,lease_expires_at=?,
                       started_at=COALESCE(started_at,?),updated_at=?
                 WHERE id=?
                """,
                (owner, expires, now, now, step_id),
            )
            delegation_status = str(delegation["status"])
            if bool(step["required"]):
                delegation_status = "executing"
                db.execute(
                    """
                    UPDATE delegations
                       SET status='executing',waiting_on='',
                           started_at=COALESCE(started_at,?),updated_at=?
                     WHERE id=?
                    """,
                    (now, now, delegation_id),
                )
            else:
                db.execute(
                    """
                    UPDATE delegations
                       SET started_at=COALESCE(started_at,?),updated_at=?
                     WHERE id=?
                    """,
                    (now, now, delegation_id),
                )
            self._event(
                db,
                delegation_id,
                expected_version,
                "delegation.step_claimed",
                actor_type="worker",
                actor_id=owner,
                from_status=delegation["status"],
                to_status=delegation_status,
                metadata={
                    "step_id": step_id,
                    "lease_expires_at": expires,
                    "required": bool(step["required"]),
                },
            )
        self.sync_projection(delegation_id)
        return Claim(delegation_id, step_id, expected_version, owner, expires)

    def renew_claim(
        self,
        delegation_id: str,
        step_id: str,
        *,
        expected_version: int,
        owner: str,
        lease_seconds: int = 900,
    ) -> Claim:
        owner = _safe_ref(owner, "owner", required=True)
        now = self.now()
        expires = now + max(30, min(int(lease_seconds), 7200))
        with self._tx() as db:
            self._version(self._require(db, delegation_id), expected_version)
            step = self._require_step(db, delegation_id, step_id, expected_version)
            if step["status"] != "executing" or step["lease_owner"] != owner:
                raise DelegationConflict("claim is not owned by this worker")
            if float(step["lease_expires_at"] or 0) <= now:
                raise DelegationConflict("claim has expired")
            db.execute(
                """
                UPDATE delegation_steps SET lease_expires_at=?,updated_at=?
                 WHERE id=?
                """,
                (expires, now, step_id),
            )
        return Claim(delegation_id, step_id, expected_version, owner, expires)

    def record_attempt(
        self,
        delegation_id: str,
        step_id: str,
        *,
        expected_version: int,
        owner: str,
        succeeded: bool,
        artifact_locator: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        owner = _safe_ref(owner, "owner", required=True)
        artifact_locator = _safe_ref(artifact_locator, "artifact_locator")
        error_code = _safe_ref(error_code, "error_code")
        now = self.now()
        with self._tx() as db:
            delegation = self._require(db, delegation_id)
            self._version(delegation, expected_version)
            step = self._require_step(db, delegation_id, step_id, expected_version)
            if step["lease_owner"] != owner:
                raise DelegationConflict("attempt worker does not own the step")
            if float(step["lease_expires_at"] or 0) <= now:
                raise DelegationConflict("attempt lease has expired")
            status = "verifying" if succeeded else "failed"
            db.execute(
                """
                UPDATE delegation_steps
                   SET status=?,attempt_count=attempt_count+1,
                       artifact_locator=?,last_error_code=?,
                       lease_owner='',lease_expires_at=NULL,updated_at=?,
                       finished_at=CASE WHEN ?='failed' THEN ? ELSE finished_at END
                 WHERE id=?
                """,
                (
                    status,
                    artifact_locator,
                    error_code,
                    now,
                    status,
                    now,
                    step_id,
                ),
            )
            delegation_status = str(delegation["status"])
            if bool(step["required"]):
                delegation_status = status
                db.execute(
                    """
                    UPDATE delegations
                       SET status=?,last_error_code=?,updated_at=?
                     WHERE id=?
                    """,
                    (status, error_code, now, delegation_id),
                )
            self._event(
                db,
                delegation_id,
                expected_version,
                "delegation.attempted",
                actor_type="worker",
                actor_id=owner,
                from_status=delegation["status"],
                to_status=delegation_status,
                reason_code=error_code,
                metadata={
                    "step_id": step_id,
                    "succeeded": bool(succeeded),
                    "required": bool(step["required"]),
                    "artifact_locator": artifact_locator,
                },
            )
            result = self._require_step(db, delegation_id, step_id, expected_version)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def record_evidence(
        self,
        delegation_id: str,
        step_id: str,
        *,
        expected_version: int,
        evidence_type: str,
        strength: str,
        authority: str,
        resource_locator: str,
        observed_digest: str,
        expected_summary: str,
        observed_summary: str,
        matched: bool,
        privacy_class: str = "private",
        expires_at: float | None = None,
        metadata: dict[str, Any] | None = None,
        actor_id: str = "",
    ) -> dict[str, Any]:
        evidence_type = _safe_ref(evidence_type, "evidence_type", required=True)
        strength = _safe_ref(strength, "strength", required=True)
        authority = _safe_ref(authority, "authority", required=True)
        resource_locator = _safe_ref(resource_locator, "resource_locator")
        observed_digest = _safe_ref(
            observed_digest, "observed_digest", required=True
        )
        expected_summary = _text(expected_summary, "expected_summary", limit=500)
        observed_summary = _text(observed_summary, "observed_summary", limit=500)
        metadata = _object(metadata or {}, "evidence metadata")
        if strength not in QUALIFYING_STRENGTHS | {"weak"}:
            raise DelegationError("invalid evidence strength")
        if privacy_class not in PRIVACY_CLASSES:
            raise DelegationError("invalid privacy_class")
        now = self.now()
        evidence_id = _new_id("dev")
        with self._tx() as db:
            delegation = self._require(db, delegation_id)
            self._version(delegation, expected_version)
            step = self._require_step(db, delegation_id, step_id, expected_version)
            policy = json.loads(delegation["verification_policy_json"])
            expected_verifier = str(
                policy.get("verifier") or step["kind"] or ""
            )
            trusted_verifier = bool(
                authority == str(delegation["authority"])
                and actor_id
                and actor_id == expected_verifier
                and step["status"] in {
                    "verifying",
                    "awaiting_external",
                    "blocked",
                }
            )
            db.execute(
                """
                INSERT INTO delegation_evidence(
                    id,delegation_id,step_id,contract_version,evidence_type,
                    strength,authority,resource_locator,observed_digest,
                    expected_summary,observed_summary,matched,observed_at,
                    expires_at,privacy_class,metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evidence_id,
                    delegation_id,
                    step_id,
                    expected_version,
                    evidence_type,
                    strength,
                    authority,
                    resource_locator,
                    observed_digest,
                    expected_summary,
                    observed_summary,
                    int(matched),
                    now,
                    float(expires_at) if expires_at is not None else None,
                    privacy_class,
                    _json(metadata),
                ),
            )
            qualifies = bool(
                matched
                and strength in QUALIFYING_STRENGTHS
                and trusted_verifier
            )
            if qualifies:
                db.execute(
                    """
                    UPDATE delegation_steps
                       SET status='completed',lease_owner='',
                           lease_expires_at=NULL,finished_at=?,updated_at=?
                     WHERE id=?
                    """,
                    (now, now, step_id),
                )
            elif step["status"] not in {"completed", "failed"}:
                db.execute(
                    """
                    UPDATE delegation_steps SET status='verifying',updated_at=?
                     WHERE id=?
                    """,
                    (now, step_id),
                )
            self._event(
                db,
                delegation_id,
                expected_version,
                "delegation.evidence_recorded",
                actor_type="verifier",
                actor_id=actor_id,
                metadata={
                    "evidence_id": evidence_id,
                    "step_id": step_id,
                    "strength": strength,
                    "matched": bool(matched),
                    "trusted_verifier": trusted_verifier,
                },
            )
            self._evaluate_tx(db, delegation_id)
            result = db.execute(
                "SELECT * FROM delegation_evidence WHERE id=?", (evidence_id,)
            ).fetchone()
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def evaluate_completion(self, delegation_id: str) -> dict[str, Any]:
        with self._tx() as db:
            self._require(db, delegation_id)
            self._evaluate_tx(db, delegation_id)
            result = self._require(db, delegation_id)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def _evaluate_tx(self, db: sqlite3.Connection, delegation_id: str) -> None:
        delegation = self._require(db, delegation_id)
        if delegation["status"] in TERMINAL_STATUSES:
            return
        version = int(delegation["contract_version"])
        steps = db.execute(
            """
            SELECT * FROM delegation_steps
             WHERE delegation_id=? AND contract_version=? AND required=1
             ORDER BY sequence
            """,
            (delegation_id, version),
        ).fetchall()
        if not steps:
            return
        if any(step["status"] != "completed" for step in steps):
            status = (
                "failed"
                if any(step["status"] == "failed" for step in steps)
                else "verifying"
            )
            if delegation["status"] not in {
                "awaiting_external",
                "needs_user",
                "needs_clarification",
                "blocked",
            }:
                db.execute(
                    "UPDATE delegations SET status=?,updated_at=? WHERE id=?",
                    (status, self.now(), delegation_id),
                )
            return
        for step in steps:
            qualifying = db.execute(
                """
                SELECT 1 FROM delegation_evidence
                 WHERE delegation_id=? AND step_id=? AND contract_version=?
                   AND matched=1
                   AND strength IN ('strong','corroborated','user_attested')
                   AND (expires_at IS NULL OR expires_at>?)
                 LIMIT 1
                """,
                (delegation_id, step["id"], version, self.now()),
            ).fetchone()
            if qualifying is None:
                return
        if delegation["waiting_on"]:
            return
        now = self.now()
        db.execute(
            """
            UPDATE delegations
               SET status='completed',verified_at=?,completed_at=?,updated_at=?,
                   last_error_code='',last_error_summary=''
             WHERE id=?
            """,
            (now, now, now, delegation_id),
        )
        self._event(
            db,
            delegation_id,
            version,
            "delegation.completed",
            from_status=delegation["status"],
            to_status="completed",
            reason_code="required_evidence_satisfied",
        )

    def mark_waiting(
        self,
        delegation_id: str,
        *,
        expected_version: int,
        waiting_on: str,
        needs_user: bool = False,
        reason_code: str = "",
    ) -> dict[str, Any]:
        waiting_on = _safe_ref(waiting_on, "waiting_on", required=True)
        reason_code = _safe_ref(reason_code, "reason_code")
        status = "needs_user" if needs_user else "awaiting_external"
        with self._tx() as db:
            current = self._require(db, delegation_id)
            self._version(current, expected_version)
            if current["status"] in TERMINAL_STATUSES:
                raise DelegationConflict("terminal delegation cannot wait")
            db.execute(
                """
                UPDATE delegations SET status=?,waiting_on=?,updated_at=?
                 WHERE id=?
                """,
                (status, waiting_on, self.now(), delegation_id),
            )
            self._event(
                db,
                delegation_id,
                expected_version,
                (
                    "delegation.needs_user"
                    if needs_user
                    else "delegation.awaiting_external"
                ),
                from_status=current["status"],
                to_status=status,
                reason_code=reason_code,
                metadata={"waiting_on": waiting_on},
            )
            result = self._require(db, delegation_id)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def resume_external(
        self,
        delegation_id: str,
        *,
        expected_version: int,
        reason_code: str = "external_event_observed",
    ) -> dict[str, Any]:
        """Resume one externally waiting contract after a correlated event."""
        reason_code = _safe_ref(reason_code, "reason_code", required=True)
        with self._tx() as db:
            current = self._require(db, delegation_id)
            self._version(current, expected_version)
            if current["status"] != "awaiting_external":
                raise DelegationConflict("delegation is not awaiting an external event")
            db.execute(
                """
                UPDATE delegations SET status='verifying',waiting_on='',updated_at=?
                 WHERE id=?
                """,
                (self.now(), delegation_id),
            )
            self._event(
                db,
                delegation_id,
                expected_version,
                "delegation.external_event_observed",
                from_status="awaiting_external",
                to_status="verifying",
                reason_code=reason_code,
            )
            result = self._require(db, delegation_id)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def confirm(
        self,
        delegation_id: str,
        *,
        expected_version: int,
        principal_id: str,
    ) -> dict[str, Any]:
        principal_id = _safe_ref(principal_id, "principal_id", required=True)
        with self._tx() as db:
            current = self._require(db, delegation_id)
            self._version(current, expected_version)
            if current["principal_id"] != principal_id:
                raise DelegationConflict("principal cannot authorize this delegation")
            if not is_confirmable(current):
                if int(current["risk_tier"]) == 4:
                    raise DelegationConflict(
                        "R4 delegation must remain human-operated"
                    )
                raise DelegationConflict(
                    "delegation is not awaiting an R3 risk confirmation"
                )
            db.execute(
                """
                UPDATE delegations
                   SET status='bound',authorized=1,waiting_on='',updated_at=?
                 WHERE id=?
                """,
                (self.now(), delegation_id),
            )
            self._event(
                db,
                delegation_id,
                expected_version,
                "delegation.confirmed",
                actor_type="principal",
                actor_id=principal_id,
                from_status=current["status"],
                to_status="bound",
            )
            result = self._require(db, delegation_id)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def retry(
        self,
        delegation_id: str,
        *,
        expected_version: int,
        actor_id: str = "",
    ) -> dict[str, Any]:
        with self._tx() as db:
            current = self._require(db, delegation_id)
            self._version(current, expected_version)
            if not is_retryable(current):
                raise DelegationConflict(f"status {current['status']} is not retryable")
            verification_recovery = bool(
                current["status"] == "needs_user"
                and current["waiting_on"] == "verification_recovery"
            )
            now = self.now()
            if not verification_recovery:
                db.execute(
                    """
                    UPDATE delegation_steps
                       SET status='pending',lease_owner='',lease_expires_at=NULL,
                           last_error_code='',started_at=NULL,finished_at=NULL,
                           updated_at=?
                     WHERE delegation_id=? AND contract_version=?
                       AND status IN ('failed','blocked','verifying')
                    """,
                    (now, delegation_id, expected_version),
                )
            next_status = "verifying" if verification_recovery else "bound"
            db.execute(
                """
                UPDATE delegations
                   SET status=?,waiting_on='',last_error_code='',
                       last_error_summary='',updated_at=?
                 WHERE id=?
                """,
                (next_status, now, delegation_id),
            )
            self._event(
                db,
                delegation_id,
                expected_version,
                "delegation.retry_requested",
                actor_type="principal",
                actor_id=actor_id,
                from_status=current["status"],
                to_status=next_status,
                reason_code=(
                    "verification_recovery"
                    if verification_recovery
                    else "execution_retry"
                ),
            )
            result = self._require(db, delegation_id)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def terminal(
        self,
        delegation_id: str,
        *,
        expected_version: int,
        status: str,
        reason_code: str,
        actor_id: str = "",
        superseded_by: str = "",
    ) -> dict[str, Any]:
        if status not in {"failed", "cancelled", "superseded"}:
            raise DelegationError("invalid requested terminal status")
        reason_code = _safe_ref(reason_code, "reason_code", required=True)
        now = self.now()
        with self._tx() as db:
            current = self._require(db, delegation_id)
            self._version(current, expected_version)
            if current["status"] in TERMINAL_STATUSES or current["status"] == status:
                result = current
            else:
                db.execute(
                    """
                    UPDATE delegation_steps
                       SET status=?,lease_owner='',lease_expires_at=NULL,
                           finished_at=?,updated_at=?
                     WHERE delegation_id=? AND contract_version=?
                       AND status NOT IN ('completed','cancelled','superseded')
                    """,
                    (status, now, now, delegation_id, expected_version),
                )
                db.execute(
                    """
                    UPDATE delegations
                       SET status=?,waiting_on='',updated_at=?,last_error_code=?
                     WHERE id=?
                    """,
                    (status, now, reason_code, delegation_id),
                )
                self._event(
                    db,
                    delegation_id,
                    expected_version,
                    f"delegation.{status}",
                    actor_type=(
                        "principal" if status == "cancelled" else "system"
                    ),
                    actor_id=actor_id,
                    from_status=current["status"],
                    to_status=status,
                    reason_code=reason_code,
                    metadata=(
                        {"superseded_by": superseded_by}
                        if superseded_by
                        else {}
                    ),
                )
                result = self._require(db, delegation_id)
        output = _row(result) or {}
        self.sync_projection(delegation_id)
        return output

    def link(
        self,
        delegation_id: str,
        entity_type: str,
        entity_id: str,
        *,
        relation: str = "projects",
    ) -> None:
        entity_type = _safe_ref(entity_type, "entity_type", required=True)
        entity_id = _safe_ref(entity_id, "entity_id", required=True)
        relation = _safe_ref(relation, "relation", required=True)
        with self._tx() as db:
            self._require(db, delegation_id)
            db.execute(
                """
                INSERT OR IGNORE INTO delegation_links(
                    delegation_id,entity_type,entity_id,relation,created_at
                ) VALUES (?,?,?,?,?)
                """,
                (delegation_id, entity_type, entity_id, relation, self.now()),
            )
        self.sync_projection(delegation_id)

    def sync_projection(self, delegation_id: str) -> dict[str, Any]:
        """Refresh user-facing projections after an authoritative transition."""
        try:
            from core.delegation_projection import sync_projection

            return sync_projection(self, delegation_id)
        except Exception as exc:
            return {
                "delegation_id": delegation_id,
                "issues": [f"projection:{exc}"],
            }

    def get(self, delegation_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            delegation = self._require(db, delegation_id)
            result = _row(delegation) or {}
            version = int(delegation["contract_version"])
            result["expected_postcondition"] = json.loads(
                result.pop("expected_postcondition_json")
            )
            result["verification_policy"] = json.loads(
                result.pop("verification_policy_json")
            )
            result["steps"] = [
                _row(row)
                for row in db.execute(
                    """
                    SELECT * FROM delegation_steps
                     WHERE delegation_id=? AND contract_version=?
                     ORDER BY sequence
                    """,
                    (delegation_id, version),
                ).fetchall()
            ]
            for step in result["steps"]:
                step["depends_on"] = json.loads(step.pop("depends_on_json"))
            result["evidence"] = [
                _row(row)
                for row in db.execute(
                    """
                    SELECT * FROM delegation_evidence
                     WHERE delegation_id=? AND contract_version=?
                     ORDER BY observed_at
                    """,
                    (delegation_id, version),
                ).fetchall()
            ]
            for evidence in result["evidence"]:
                evidence["metadata"] = json.loads(evidence.pop("metadata_json"))
            result["events"] = [
                _row(row)
                for row in db.execute(
                    """
                    SELECT * FROM delegation_events WHERE delegation_id=?
                     ORDER BY created_at,rowid
                    """,
                    (delegation_id,),
                ).fetchall()
            ]
            for event in result["events"]:
                event["metadata"] = json.loads(event.pop("metadata_json"))
            result["links"] = [
                _row(row)
                for row in db.execute(
                    """
                    SELECT entity_type,entity_id,relation,created_at
                      FROM delegation_links WHERE delegation_id=?
                    """,
                    (delegation_id,),
                ).fetchall()
            ]
            return result

    def list(
        self,
        *,
        status: str = "",
        matter_id: str = "",
        needs_attention: bool = False,
        include_shadow: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if not include_shadow:
            clauses.append("capture_mode!='shadow'")
        if status:
            if status not in ALL_STATUSES:
                raise DelegationError("invalid status filter")
            clauses.append("status=?")
            values.append(status)
        if matter_id:
            clauses.append("matter_id=?")
            values.append(_safe_ref(matter_id, "matter_id"))
        if needs_attention:
            clauses.append("status IN ('needs_user','needs_clarification')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit), 500)))
        with closing(self._connect()) as db:
            rows = db.execute(
                f"""
                SELECT * FROM delegations {where}
                 ORDER BY
                   CASE status
                     WHEN 'needs_user' THEN 0
                     WHEN 'needs_clarification' THEN 1
                     WHEN 'failed' THEN 2
                     WHEN 'awaiting_external' THEN 3
                     ELSE 4
                   END,
                   updated_at DESC
                 LIMIT ?
                """,
                values,
            ).fetchall()
            result = [_row(row) or {} for row in rows]
        for item in result:
            item["expected_postcondition"] = json.loads(
                item.pop("expected_postcondition_json")
            )
            item["verification_policy"] = json.loads(
                item.pop("verification_policy_json")
            )
        return result

    def label_shadow(
        self,
        delegation_id: str,
        *,
        actual_is_delegation: bool,
        actual_target_risk: int,
        actual_verifier: str,
    ) -> None:
        """Attach a human-reviewed Phase-0 label without changing execution."""
        actual_verifier = _safe_ref(actual_verifier, "actual_verifier")
        actual_target_risk = int(actual_target_risk)
        if actual_target_risk < 0 or actual_target_risk > 4:
            raise DelegationError("actual_target_risk must be in R0-R4")
        with self._tx() as db:
            delegation = self._require(db, delegation_id)
            if delegation["capture_mode"] != "shadow":
                raise DelegationConflict("only shadow delegations can be labeled")
            updated = db.execute(
                """
                UPDATE delegation_shadow_labels
                   SET actual_is_delegation=?,actual_target_risk=?,
                       actual_verifier=?,labeled_at=?
                 WHERE delegation_id=?
                """,
                (
                    int(actual_is_delegation),
                    actual_target_risk,
                    actual_verifier,
                    self.now(),
                    delegation_id,
                ),
            )
            if updated.rowcount != 1:
                raise DelegationNotFound("shadow prediction row is missing")
            self._event(
                db,
                delegation_id,
                int(delegation["contract_version"]),
                "delegation.shadow_labeled",
                actor_type="reviewer",
                metadata={
                    "actual_is_delegation": bool(actual_is_delegation),
                    "actual_target_risk": actual_target_risk,
                    "actual_verifier": actual_verifier,
                },
            )

    def shadow_metrics(self) -> dict[str, Any]:
        """Return measurable Phase-0 gates; unlabeled rows are kept separate."""
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT l.*,d.source,d.operation,d.created_at
                  FROM delegation_shadow_labels l
                  JOIN delegations d ON d.id=l.delegation_id
                 WHERE actual_is_delegation IS NOT NULL
                """
            ).fetchall()
            total_predictions = int(
                db.execute(
                    "SELECT COUNT(*) AS count FROM delegation_shadow_labels"
                ).fetchone()["count"]
            )
        labeled = len(rows)
        true_positive = sum(
            1
            for row in rows
            if row["predicted_is_delegation"] and row["actual_is_delegation"]
        )
        false_positive = sum(
            1
            for row in rows
            if row["predicted_is_delegation"] and not row["actual_is_delegation"]
        )
        false_negative = sum(
            1
            for row in rows
            if not row["predicted_is_delegation"] and row["actual_is_delegation"]
        )
        risk_positive = sum(
            1 for row in rows if int(row["actual_target_risk"] or 0) >= 3
        )
        risk_caught = sum(
            1
            for row in rows
            if int(row["actual_target_risk"] or 0) >= 3
            and int(row["predicted_target_risk"] or 0) >= 3
        )
        verifier_match = sum(
            1
            for row in rows
            if str(row["predicted_verifier"] or "")
            == str(row["actual_verifier"] or "")
        )
        observed_times = [
            float(row["created_at"])
            for row in rows
            if row["created_at"] is not None
        ]
        observation_days = (
            (max(observed_times) - min(observed_times)) / 86400
            if len(observed_times) >= 2
            else 0.0
        )
        connector_classes = {
            str(row["operation"] or "")
            for row in rows
            if str(row["operation"] or "")
        }
        return {
            "predictions": total_predictions,
            "labeled": labeled,
            "precision": (
                true_positive / (true_positive + false_positive)
                if true_positive + false_positive
                else None
            ),
            "recall": (
                true_positive / (true_positive + false_negative)
                if true_positive + false_negative
                else None
            ),
            "high_risk_recall": (
                risk_caught / risk_positive if risk_positive else None
            ),
            "verifier_accuracy": verifier_match / labeled if labeled else None,
            "observation_days": observation_days,
            "connector_classes": sorted(connector_classes),
            "connector_class_count": len(connector_classes),
            "phase1_ready": bool(
                labeled >= 50
                and observation_days >= 14
                and len(connector_classes) >= 5
                and false_positive + true_positive > 0
                and true_positive / (true_positive + false_positive) >= 0.95
                and (not risk_positive or risk_caught / risk_positive >= 0.95)
                and verifier_match / labeled >= 0.95
            ),
        }

    def release_expired_leases(self, *, limit: int = 100) -> list[str]:
        """Release only currently expired active leases; never scan history."""
        now = self.now()
        released: list[str] = []
        with self._tx() as db:
            rows = db.execute(
                """
                SELECT s.*,d.status AS delegation_status
                  FROM delegation_steps s
                  JOIN delegations d ON d.id=s.delegation_id
                 WHERE s.status='executing'
                   AND s.lease_expires_at IS NOT NULL
                   AND s.lease_expires_at<=?
                   AND d.status NOT IN ('completed','cancelled','superseded')
                 ORDER BY s.lease_expires_at
                 LIMIT ?
                """,
                (now, max(1, min(int(limit), 500))),
            ).fetchall()
            for step in rows:
                db.execute(
                    """
                    UPDATE delegation_steps
                       SET status='pending',lease_owner='',lease_expires_at=NULL,
                           updated_at=?
                     WHERE id=?
                    """,
                    (now, step["id"]),
                )
                db.execute(
                    """
                    UPDATE delegations SET status='bound',updated_at=?
                     WHERE id=? AND status='executing'
                    """,
                    (now, step["delegation_id"]),
                )
                self._event(
                    db,
                    step["delegation_id"],
                    int(step["contract_version"]),
                    "delegation.lease_expired",
                    actor_type="system",
                    actor_id=step["lease_owner"],
                    from_status="executing",
                    to_status="bound",
                    reason_code="lease_expired",
                    metadata={"step_id": step["id"]},
                )
                released.append(str(step["id"]))
        for step_id in released:
            # Keep Matter next-actions in sync after an abandoned worker lease
            # returns the contract to the executable queue.
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT delegation_id FROM delegation_steps WHERE id=?",
                    (step_id,),
                ).fetchone()
            if row is not None:
                self.sync_projection(str(row["delegation_id"]))
        return released

    def metrics(self) -> dict[str, Any]:
        now = self.now()
        with closing(self._connect()) as db:
            by_status = {
                str(row["status"]): int(row["count"])
                for row in db.execute(
                    """
                    SELECT status,COUNT(*) AS count FROM delegations
                     WHERE capture_mode!='shadow' GROUP BY status
                    """
                ).fetchall()
            }
            total = sum(by_status.values())
            qualifying = int(
                db.execute(
                    """
                    SELECT COUNT(DISTINCT delegation_id) AS count
                      FROM delegation_evidence
                      JOIN delegations
                        ON delegations.id=delegation_evidence.delegation_id
                     WHERE matched=1
                       AND strength IN ('strong','corroborated','user_attested')
                       AND delegations.capture_mode!='shadow'
                    """
                ).fetchone()["count"]
            )
            overdue = int(
                db.execute(
                    """
                    SELECT COUNT(*) AS count FROM delegations
                     WHERE deadline_at IS NOT NULL AND deadline_at<?
                       AND status NOT IN ('completed','cancelled','superseded')
                       AND capture_mode!='shadow'
                    """,
                    (now,),
                ).fetchone()["count"]
            )
            verifying_age = db.execute(
                """
                SELECT COALESCE(MAX(?-updated_at),0) AS age FROM delegations
                 WHERE status='verifying' AND capture_mode!='shadow'
                """,
                (now,),
            ).fetchone()["age"]
            duplicate_keys = int(
                db.execute(
                    """
                    SELECT COUNT(*) AS count FROM (
                        SELECT idempotency_key FROM delegation_steps
                         GROUP BY idempotency_key HAVING COUNT(*)>1
                    )
                    """
                ).fetchone()["count"]
            )
            mismatches = db.execute(
                """
                SELECT expected_summary,observed_summary
                  FROM delegation_evidence
                  JOIN delegations
                    ON delegations.id=delegation_evidence.delegation_id
                 WHERE matched=0 AND delegations.capture_mode!='shadow'
                """
            ).fetchall()
            target_keys = {
                "target_id",
                "recipient_id",
                "receiver_id",
                "agent_id",
                "document_id",
                "event_id",
                "message_id",
            }
            wrong_target = 0
            for row in mismatches:
                try:
                    expected = json.loads(row["expected_summary"] or "{}")
                    observed = json.loads(row["observed_summary"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(expected, dict) or not isinstance(observed, dict):
                    continue
                comparable = target_keys & expected.keys() & observed.keys()
                if any(
                    str(expected[key]) != str(observed[key])
                    for key in comparable
                ):
                    wrong_target += 1
            duplicate_mutations = int(
                db.execute(
                    """
                    SELECT COUNT(*) AS count FROM (
                        SELECT delegation_id,step_id
                          FROM delegation_evidence
                         WHERE matched=1
                           AND strength IN (
                               'strong','corroborated','user_attested'
                           )
                           AND resource_locator!=''
                         GROUP BY delegation_id,step_id
                        HAVING COUNT(DISTINCT resource_locator)>1
                    )
                    """
                ).fetchone()["count"]
            )

            table_names = {
                str(row["name"])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            queue_depth = queue_failures = 0
            if "delivery_envelopes" in table_names:
                queue_depth = int(
                    db.execute(
                        """
                        SELECT COUNT(*) AS count FROM delivery_envelopes
                         WHERE state IN ('queued','attempting')
                        """
                    ).fetchone()["count"]
                )
                queue_failures = int(
                    db.execute(
                        """
                        SELECT COUNT(*) AS count FROM delivery_envelopes
                         WHERE state='failed'
                        """
                    ).fetchone()["count"]
                )
            oldest_handoff = stale_handoffs = 0
            if "surface_handoffs" in table_names:
                oldest_handoff = int(
                    db.execute(
                        """
                        SELECT COALESCE(MAX(?-created_epoch),0) AS age
                          FROM surface_handoffs
                         WHERE status IN ('open','claimed')
                        """,
                        (now,),
                    ).fetchone()["age"] or 0
                )
                stale_handoffs = int(
                    db.execute(
                        """
                        SELECT COUNT(*) AS count FROM surface_handoffs
                         WHERE status IN ('open','claimed')
                           AND created_epoch<?
                        """,
                        (now - 86400,),
                    ).fetchone()["count"]
                )
        return {
            "total": total,
            "by_status": by_status,
            "completion_rate": (
                by_status.get("completed", 0) / total if total else 0.0
            ),
            "with_qualifying_evidence": qualifying,
            "overdue_active": overdue,
            "oldest_verifying_seconds": int(verifying_age or 0),
            "duplicate_idempotency_keys": duplicate_keys,
            "wrong_target_actions": wrong_target,
            "duplicate_external_mutations": duplicate_mutations,
            "attention_asks": (
                by_status.get("needs_user", 0)
                + by_status.get("needs_clarification", 0)
            ),
            "delivery_queue_depth": queue_depth,
            "delivery_queue_failures": queue_failures,
            "oldest_handoff_seconds": oldest_handoff,
            "stale_handoffs": stale_handoffs,
        }

    @staticmethod
    def _require(db: sqlite3.Connection, delegation_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM delegations WHERE id=?", (delegation_id,)
        ).fetchone()
        if row is None:
            raise DelegationNotFound(f"delegation {delegation_id} not found")
        return row

    @staticmethod
    def _require_step(
        db: sqlite3.Connection,
        delegation_id: str,
        step_id: str,
        version: int,
    ) -> sqlite3.Row:
        row = db.execute(
            """
            SELECT * FROM delegation_steps
             WHERE id=? AND delegation_id=? AND contract_version=?
            """,
            (step_id, delegation_id, version),
        ).fetchone()
        if row is None:
            raise DelegationNotFound(f"step {step_id} not found in current contract")
        return row

    @staticmethod
    def _version(row: sqlite3.Row, expected: int) -> None:
        actual = int(row["contract_version"])
        if actual != int(expected):
            raise DelegationConflict(
                f"contract version conflict: expected {expected}, actual {actual}"
            )
