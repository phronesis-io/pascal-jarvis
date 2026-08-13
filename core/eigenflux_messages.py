"""Verified EigenFlux direct messages.

The model supplies a human-readable friend name or remark.  This module owns
the unstable parts that must not be copied by an LLM:

* resolve the current server-side friend record to its stable agent ID;
* reserve an idempotency key before the external mutation;
* reconcile an interrupted attempt from conversation history;
* verify the returned message against the authoritative history endpoint.

Only hashes and external receipt IDs are persisted.  Message content is not.
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
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from core.person_registry import (
    PersonNotFound,
    PersonRegistry,
    PersonRegistryError,
)


class EigenFluxMessageError(RuntimeError):
    """Base error for a message that could not be safely completed."""


class RecipientNotFound(EigenFluxMessageError):
    """The supplied human label did not identify a current friend."""


class RecipientAmbiguous(EigenFluxMessageError):
    """The supplied human label identified more than one friend."""


class CliFailure(EigenFluxMessageError):
    """The EigenFlux CLI did not return a successful JSON response."""


@dataclass(frozen=True, slots=True)
class Friend:
    agent_id: str
    agent_name: str
    remark: str = ""


@dataclass(frozen=True, slots=True)
class MessageReceipt:
    state: str
    recipient_name: str
    recipient_id: str
    idempotency_key: str = ""
    msg_id: str = ""
    conv_id: str = ""
    duplicate: bool = False
    detail: str = ""

    @property
    def completed(self) -> bool:
        return self.state == "verified"

    def human_text(self) -> str:
        receipt = f"（回执 …{self.msg_id[-8:]}）" if self.msg_id else ""
        if self.state == "verified" and self.duplicate:
            return (
                f"✅ 这条已发送并核验过，未重复发送："
                f"{self.recipient_name}{receipt}"
            )
        if self.state == "verified":
            return f"✅ 已核验发送给 {self.recipient_name}{receipt}"
        if self.state in {"attempting", "verifying"}:
            return (
                f"⚠️ 发送结果仍在核验，未自动重复发送。"
                f"目标：{self.recipient_name}"
            )
        return f"❌ 未发送给 {self.recipient_name}：{self.detail or '执行失败'}"


Runner = Callable[..., subprocess.CompletedProcess[str]]
ApiSender = Callable[[str, str], dict]


def _resolve_eigenflux_home(value: str | Path | None = None) -> Path:
    """Mirror EigenFlux CLI's --homedir/EIGENFLUX_HOME suffix semantics."""
    raw = value or os.environ.get("EIGENFLUX_HOME")
    home = (
        Path(raw).expanduser()
        if raw
        else Path.home() / ".eigenflux"
    )
    return home if home.name == ".eigenflux" else home / ".eigenflux"


def _durable_failure(exc: BaseException, operation: str) -> str:
    """Return diagnostic metadata that cannot contain a submitted payload."""
    status = re.search(
        r"\bHTTP\s+([1-5])(?:\d{2}|xx)\b",
        str(exc),
        flags=re.IGNORECASE,
    )
    if status:
        return f"EigenFlux {operation} failed: HTTP {status.group(1)}xx"
    return f"EigenFlux {operation} failed ({type(exc).__name__})"


def _normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(normalized.split())


def _payload_list(obj: dict, key: str) -> list[dict]:
    value = obj.get(key)
    if value is None and isinstance(obj.get("data"), dict):
        value = obj["data"].get(key)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _response_data(obj: dict) -> dict:
    value = obj.get("data")
    return value if isinstance(value, dict) else obj


def _next_cursor(obj: dict) -> str:
    data = _response_data(obj)
    cursor = str(
        data.get("next_cursor")
        or data.get("nextCursor")
        or obj.get("next_cursor")
        or obj.get("nextCursor")
        or ""
    ).strip()
    return "" if cursor == "0" else cursor


class EigenFluxApiClient:
    """Small authenticated client for payload-bearing operations.

    Message content is encoded in the HTTPS request body. It never appears in
    a child process argument, shell history, or process listing.
    """

    def __init__(
        self,
        home: str | Path | None = None,
        *,
        opener: Callable = urllib.request.urlopen,
    ):
        self.home = _resolve_eigenflux_home(home)
        self.opener = opener

    def _connection(self) -> tuple[str, str]:
        try:
            config = json.loads(
                (self.home / "config.json").read_text(encoding="utf-8")
            )
            server_name = str(config.get("default_server") or "").strip()
            server = next(
                row
                for row in config.get("servers", [])
                if str(row.get("name") or "") == server_name
            )
            endpoint = str(server.get("endpoint") or "").rstrip("/")
            credentials = json.loads(
                (
                    self.home
                    / "servers"
                    / server_name
                    / "credentials.json"
                ).read_text(encoding="utf-8")
            )
            token = str(credentials.get("access_token") or "").strip()
        except (OSError, ValueError, TypeError, StopIteration) as exc:
            raise CliFailure("EigenFlux authentication is not configured") from exc
        if not endpoint.startswith("https://") or not token:
            raise CliFailure("EigenFlux endpoint or access token is invalid")
        return endpoint, token

    def send(self, receiver_id: str, content: str) -> dict:
        endpoint, token = self._connection()
        body = json.dumps(
            {"receiver_id": str(receiver_id), "content": str(content)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{endpoint}/api/v1/pm/send",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise CliFailure(
                f"EigenFlux API returned HTTP {int(exc.code) // 100}xx"
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise CliFailure(f"EigenFlux API send failed: {exc}") from exc
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CliFailure("EigenFlux API returned invalid JSON") from exc
        if not isinstance(obj, dict):
            raise CliFailure("EigenFlux API returned a non-object response")
        code = obj.get("code")
        if code not in (None, 0, "0"):
            safe_code = str(code)
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", safe_code):
                safe_code = "nonzero"
            raise CliFailure(
                f"EigenFlux API returned application error code {safe_code}"
            )
        return obj


class EigenFluxMessenger:
    """Deterministic friend resolver, sender, and receipt verifier."""

    def __init__(
        self,
        root: str | Path | None = None,
        db_path: str | Path | None = None,
        bindings_path: str | Path | None = None,
        runner: Runner = subprocess.run,
        api_sender: ApiSender | None = None,
        now: Callable[[], float] = time.time,
        attempt_stale_seconds: int = 60,
        verification_clock_skew_seconds: int = 600,
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
        self.bindings_path = Path(
            bindings_path
            or self.root / "data" / "eigenflux_contact_bindings.json"
        )
        self.runner = runner
        self.api_sender = api_sender or EigenFluxApiClient().send
        self.now = now
        self.attempt_stale_seconds = attempt_stale_seconds
        self.verification_clock_skew_seconds = max(
            0, int(verification_clock_skew_seconds))

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS verified_external_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    contract_version TEXT NOT NULL DEFAULT 'v1',
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    conv_id TEXT NOT NULL DEFAULT '',
                    msg_id TEXT NOT NULL DEFAULT '',
                    created_epoch REAL NOT NULL,
                    updated_epoch REAL NOT NULL,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_verified_actions_state
                    ON verified_external_actions(state, updated_epoch);
                """
            )
            # Older builds could bind the same server receipt to multiple
            # explicit-repeat contracts. Preserve the earliest claim and
            # reopen every duplicate before installing the invariant.
            db.execute("BEGIN IMMEDIATE")
            duplicate_ids = db.execute(
                """
                SELECT msg_id FROM verified_external_actions
                 WHERE action_type='eigenflux_message' AND msg_id != ''
                 GROUP BY msg_id HAVING COUNT(*) > 1
                """
            ).fetchall()
            reopened_delegations: set[str] = set()
            for duplicate in duplicate_ids:
                claims = db.execute(
                    """
                    SELECT idempotency_key FROM verified_external_actions
                     WHERE action_type='eigenflux_message' AND msg_id=?
                     ORDER BY created_epoch, idempotency_key
                    """,
                    (duplicate["msg_id"],),
                ).fetchall()
                for claim in claims[1:]:
                    action_key = str(claim["idempotency_key"])
                    now = self.now()
                    db.execute(
                        """
                        UPDATE verified_external_actions
                           SET state='verifying',conv_id='',msg_id='',
                               last_error='duplicate receipt claim released',
                               updated_epoch=?
                         WHERE idempotency_key=?
                        """,
                        (now, action_key),
                    )
                    reopened_delegations.update(
                        self._reopen_duplicate_projection(
                            db, action_key, now
                        )
                    )
            released_keys = {
                str(row["idempotency_key"])
                for row in db.execute(
                    """
                    SELECT idempotency_key
                      FROM verified_external_actions
                     WHERE action_type='eigenflux_message'
                       AND state='verifying' AND msg_id=''
                       AND last_error='duplicate receipt claim released'
                    """
                ).fetchall()
            }
            tables = {
                str(row["name"])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "delegations" in tables:
                released_keys.update(
                    str(row["idempotency_key"])
                    for row in db.execute(
                        """
                        SELECT a.idempotency_key
                          FROM verified_external_actions AS a
                          JOIN delegations AS d
                            ON d.source='eigenflux-message'
                           AND d.source_ref=(
                               'attempt:' || a.idempotency_key
                           )
                         WHERE a.action_type='eigenflux_message'
                           AND a.state!='verified'
                           AND d.status='completed'
                        """
                    ).fetchall()
                )
            for action_key in sorted(released_keys):
                reopened_delegations.update(
                    self._reopen_duplicate_projection(
                        db, action_key, self.now()
                    )
                )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_verified_actions_unique_message_receipt
                    ON verified_external_actions(msg_id)
                    WHERE action_type='eigenflux_message' AND msg_id != ''
                """
            )
            db.commit()
            if reopened_delegations:
                try:
                    from core.delegations import DelegationStore

                    store = DelegationStore(
                        root=self.root, db_path=self.db_path
                    )
                    for delegation_id in sorted(reopened_delegations):
                        store.sync_projection(delegation_id)
                except Exception:
                    # Canonical state and a durable projection retry were
                    # committed in the migration transaction.
                    pass
            return db
        except Exception:
            db.close()
            raise

    @staticmethod
    def _reopen_duplicate_projection(
        db: sqlite3.Connection, action_key: str, now: float
    ) -> set[str]:
        """Withdraw invalid completion evidence linked to a released receipt."""
        tables = {
            str(row["name"])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "delegations",
            "delegation_steps",
            "delegation_evidence",
            "delegation_events",
        }
        if not required.issubset(tables):
            return set()
        rows = db.execute(
            """
            SELECT id,status,contract_version,completed_at,
                   verification_policy_json
              FROM delegations
             WHERE source='eigenflux-message' AND source_ref=?
            """,
            (f"attempt:{action_key}",),
        ).fetchall()
        reopened: set[str] = set()
        for delegation in rows:
            if str(delegation["status"]) != "completed":
                continue
            delegation_id = str(delegation["id"])
            version = int(delegation["contract_version"])
            terminal_at = (
                float(delegation["completed_at"])
                if delegation["completed_at"] is not None
                else None
            )
            try:
                policy = json.loads(
                    str(delegation["verification_policy_json"] or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                policy = {}
            if not isinstance(policy, dict):
                policy = {}
            policy["msg_id"] = ""
            step_ids = [
                str(row["id"])
                for row in db.execute(
                    """
                    SELECT id FROM delegation_steps
                     WHERE delegation_id=? AND contract_version=? AND required=1
                    """,
                    (delegation_id, version),
                ).fetchall()
            ]
            if step_ids:
                placeholders = ",".join("?" for _ in step_ids)
                db.execute(
                    f"""
                    UPDATE delegation_evidence
                       SET trusted=0,expires_at=?
                     WHERE delegation_id=? AND contract_version=?
                       AND step_id IN ({placeholders})
                    """,
                    (now, delegation_id, version, *step_ids),
                )
                db.execute(
                    f"""
                    UPDATE delegation_steps
                       SET status='verifying',finished_at=NULL,
                           lease_owner='',lease_expires_at=NULL,
                           artifact_locator='',
                           last_error_code='duplicate_receipt_released',
                           updated_at=?
                     WHERE id IN ({placeholders})
                    """,
                    (now, *step_ids),
                )
            db.execute(
                """
                UPDATE delegations
                   SET status='verifying',verified_at=NULL,completed_at=NULL,
                       waiting_on='',last_error_code='duplicate_receipt_released',
                       last_error_summary='Receipt authority was withdrawn',
                       verification_policy_json=?,
                       updated_at=?
                 WHERE id=?
                """,
                (
                    json.dumps(
                        policy,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                    delegation_id,
                ),
            )
            db.execute(
                """
                INSERT INTO delegation_events(
                    event_id,delegation_id,contract_version,event_type,
                    actor_type,actor_id,from_status,to_status,reason_code,
                    created_at,metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"dle_{uuid.uuid4().hex[:20]}",
                    delegation_id,
                    version,
                    "delegation.evidence_invalidated",
                    "system",
                    "eigenflux-receipt-migration",
                    "completed",
                    "verifying",
                    "duplicate_receipt_released",
                    now,
                    json.dumps(
                        {
                            "idempotency_key": action_key,
                            "terminal_at": terminal_at,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            if "delegation_projection_queue" in tables:
                db.execute(
                    """
                    INSERT INTO delegation_projection_queue(
                        delegation_id,attempt_count,last_error,
                        first_failed_at,updated_at
                    ) VALUES (?,?,?,?,?)
                    ON CONFLICT(delegation_id) DO UPDATE SET
                        attempt_count=attempt_count+1,
                        last_error=excluded.last_error,
                        updated_at=excluded.updated_at
                    """,
                    (
                        delegation_id,
                        1,
                        "duplicate EigenFlux receipt authority withdrawn",
                        now,
                        now,
                    ),
                )
            reopened.add(delegation_id)
        return reopened

    def _run_json(self, command: list[str], timeout: int = 30) -> dict:
        try:
            result = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.root),
            )
        except subprocess.TimeoutExpired as exc:
            raise CliFailure(f"{command[1]} timed out") from exc
        except Exception as exc:
            raise CliFailure(f"{command[1]} failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "CLI failed").strip()
            raise CliFailure(detail[:300])
        try:
            obj = json.loads(result.stdout or "")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CliFailure("CLI returned invalid JSON") from exc
        if not isinstance(obj, dict):
            raise CliFailure("CLI returned a non-object response")
        code = obj.get("code")
        if code not in (None, 0, "0"):
            raise CliFailure(str(obj.get("msg") or f"CLI code {code}")[:300])
        return obj

    def _paged_payload(
        self,
        command: list[str],
        key: str,
        *,
        max_pages: int = 20,
    ) -> list[dict]:
        rows: list[dict] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            page_command = [*command]
            if cursor:
                page_command.extend(["--cursor", cursor])
            page_command.extend(["-f", "json", "--no-interactive"])
            obj = self._run_json(page_command)
            rows.extend(_payload_list(obj, key))
            next_cursor = _next_cursor(obj)
            if not next_cursor:
                return rows
            if next_cursor in seen_cursors:
                raise CliFailure(f"{key} pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise CliFailure(f"{key} pagination exceeded {max_pages} pages")

    def list_friends(self) -> list[Friend]:
        friends: list[Friend] = []
        seen: set[str] = set()
        rows = self._paged_payload(
            ["eigenflux", "relation", "friends", "--limit", "100"],
            "friends",
        )
        for row in rows:
            agent_id = str(row.get("agent_id") or "").strip()
            agent_name = str(row.get("agent_name") or "").strip()
            if agent_id and agent_name and agent_id not in seen:
                seen.add(agent_id)
                friends.append(
                    Friend(
                        agent_id=agent_id,
                        agent_name=agent_name,
                        remark=str(row.get("remark") or "").strip(),
                    )
                )
        return friends

    def _load_binding(self, query: str) -> str | dict | None:
        registry = PersonRegistry(root=self.root)
        if registry.configured:
            try:
                person = registry.resolve(query)
            except PersonNotFound:
                # The registry is authoritative for aliases once configured.
                # Unknown input may still exact-match the live friend list,
                # but a removed alias cannot revive through the legacy file.
                return None
            except PersonRegistryError as exc:
                raise RecipientNotFound("私人人物登记册不可用，未发送") from exc
            else:
                binding = person.channels.get("eigenflux")
                if not binding:
                    raise RecipientNotFound(
                        f"“{person.name}”没有已验证的 EigenFlux 身份，未发送"
                    )
                return dict(binding)
        try:
            data = json.loads(self.bindings_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        bindings = data.get("bindings", data) if isinstance(data, dict) else {}
        if not isinstance(bindings, dict):
            return None
        wanted = _normalize_label(query)
        for alias, target in bindings.items():
            if _normalize_label(str(alias)) == wanted:
                return target
        return None

    def resolve_friend(self, query: str) -> Friend:
        raw_query = str(query or "").strip()
        if not raw_query:
            raise RecipientNotFound("收件人为空")
        if raw_query.isdigit():
            raise RecipientNotFound(
                "请使用好友名或备注，不接受模型手写的数字 agent ID"
            )

        friends = self.list_friends()
        binding = self._load_binding(raw_query)
        bound_id = ""
        target_label = raw_query
        if isinstance(binding, str):
            target_label = binding.strip()
        elif isinstance(binding, dict):
            bound_id = str(binding.get("agent_id") or "").strip()
            target_label = str(
                binding.get("agent_name")
                or binding.get("remark")
                or raw_query
            ).strip()

        if bound_id:
            candidates = [friend for friend in friends if friend.agent_id == bound_id]
            if target_label and target_label != raw_query:
                label = _normalize_label(target_label)
                candidates = [
                    friend
                    for friend in candidates
                    if label
                    in {
                        _normalize_label(friend.agent_name),
                        _normalize_label(friend.remark),
                    }
                ]
        else:
            label = _normalize_label(target_label)
            candidates = [
                friend
                for friend in friends
                if label
                in {
                    _normalize_label(friend.agent_name),
                    _normalize_label(friend.remark),
                }
            ]

        unique = {friend.agent_id: friend for friend in candidates}
        candidates = list(unique.values())
        if not candidates:
            raise RecipientNotFound(
                f"当前好友列表中没有精确匹配“{raw_query}”的对象，未发送"
            )
        if len(candidates) > 1:
            names = "、".join(friend.agent_name for friend in candidates[:4])
            raise RecipientAmbiguous(
                f"“{raw_query}”匹配到多个好友（{names}），未发送"
            )
        return candidates[0]

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _action_key(
        target_id: str, payload_hash: str, contract_version: str
    ) -> str:
        material = (
            f"owner\0eigenflux_message\0{target_id}\0"
            f"{payload_hash}\0{contract_version}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _history_messages(self, conv_id: str) -> list[dict]:
        return self._paged_payload(
            [
                "eigenflux",
                "msg",
                "history",
                "--conv-id",
                conv_id,
                "--limit",
                "100",
            ],
            "messages",
            max_pages=10,
        )

    def _candidate_conversations(self, target_id: str) -> list[str]:
        rows = self._paged_payload(
            [
                "eigenflux",
                "msg",
                "conversations",
                "--limit",
                "100",
            ],
            "conversations",
        )
        result: list[str] = []
        seen: set[str] = set()
        for row in rows:
            participants = {
                str(row.get("participant_a") or ""),
                str(row.get("participant_b") or ""),
            }
            if target_id in participants:
                conv_id = str(row.get("conv_id") or "").strip()
                if conv_id and conv_id not in seen:
                    seen.add(conv_id)
                    result.append(conv_id)
        return result

    def _find_verified_message(
        self,
        friend: Friend,
        content: str,
        created_after: float,
        conv_id: str = "",
        msg_id: str = "",
        action_key: str = "",
    ) -> tuple[str, str] | None:
        return self._find_verified_hash(
            friend.agent_id,
            self._content_hash(content),
            created_after,
            conv_id=conv_id,
            msg_id=msg_id,
            action_key=action_key,
        )

    def _find_verified_hash(
        self,
        target_id: str,
        payload_hash: str,
        created_after: float,
        *,
        conv_id: str = "",
        msg_id: str = "",
        action_key: str = "",
    ) -> tuple[str, str] | None:
        bound_receipts: set[str] = set()
        if action_key:
            with closing(self._connect()) as db:
                bound_receipts = {
                    str(row["msg_id"])
                    for row in db.execute(
                        """
                        SELECT msg_id FROM verified_external_actions
                         WHERE idempotency_key != ? AND msg_id != ''
                        """,
                        (action_key,),
                    )
                }
        conversations = [conv_id] if conv_id else self._candidate_conversations(
            target_id
        )
        for candidate_conv in conversations:
            for row in self._history_messages(candidate_conv):
                row_msg_id = str(row.get("msg_id") or "").strip()
                if not row_msg_id or row_msg_id in bound_receipts:
                    continue
                if msg_id and row_msg_id != msg_id:
                    continue
                if str(row.get("receiver_id") or "") != target_id:
                    continue
                if (
                    self._content_hash(str(row.get("content") or ""))
                    != payload_hash
                ):
                    continue
                created = float(row.get("created_at") or 0)
                if created > 10_000_000_000:
                    created /= 1000
                if not msg_id and created_after and created + 5 < created_after:
                    continue
                if action_key and not self._claim_verified_receipt(
                    action_key, candidate_conv, row_msg_id
                ):
                    # Another concurrent explicit-repeat action won this
                    # receipt. Continue through history and atomically claim a
                    # different matching server message.
                    continue
                return candidate_conv, row_msg_id
        return None

    def _claim_verified_receipt(
        self, key: str, conv_id: str, msg_id: str
    ) -> bool:
        """Atomically bind one server receipt to exactly one action contract."""
        if not key or not msg_id:
            return False
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                owner = db.execute(
                    """
                    SELECT idempotency_key FROM verified_external_actions
                     WHERE action_type='eigenflux_message' AND msg_id=?
                    """,
                    (msg_id,),
                ).fetchone()
                if owner is not None and owner["idempotency_key"] != key:
                    db.rollback()
                    return False
                updated = db.execute(
                    """
                    UPDATE verified_external_actions
                       SET state='verified',conv_id=?,msg_id=?,last_error='',
                           updated_epoch=?
                     WHERE idempotency_key=?
                       AND action_type='eigenflux_message'
                    """,
                    (conv_id, msg_id, self.now(), key),
                )
                if updated.rowcount != 1:
                    db.rollback()
                    return False
                db.commit()
                return True
            except sqlite3.IntegrityError:
                db.rollback()
                return False

    def reconcile_action(self, idempotency_key: str) -> MessageReceipt:
        """Re-read one uncertain send from authoritative conversation history."""
        key = str(idempotency_key or "").strip()
        if not key:
            raise CliFailure("idempotency key is required")
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT * FROM verified_external_actions
                 WHERE idempotency_key=?
                """,
                (key,),
            ).fetchone()
        if row is None or str(row["action_type"]) != "eigenflux_message":
            raise CliFailure("EigenFlux action was not found")
        friend = Friend(
            agent_id=str(row["target_id"]),
            agent_name=str(row["target_name"]),
        )
        if str(row["state"]) == "verified":
            return MessageReceipt(
                state="verified",
                recipient_name=friend.agent_name,
                recipient_id=friend.agent_id,
                idempotency_key=key,
                msg_id=str(row["msg_id"]),
                conv_id=str(row["conv_id"]),
                duplicate=True,
            )
        found = self._find_verified_hash(
            friend.agent_id,
            str(row["payload_hash"]),
            (
                0
                if str(row["msg_id"] or "")
                else float(row["created_epoch"])
                - self.verification_clock_skew_seconds
            ),
            conv_id=str(row["conv_id"] or ""),
            msg_id=str(row["msg_id"] or ""),
            action_key=key,
        )
        if found:
            return self._verified_receipt(
                friend, key, found[0], found[1], duplicate=True
            )
        detail = "权威历史中未找到目标一致的消息"
        self._write_state(
            key,
            state="verifying",
            conv_id=str(row["conv_id"] or ""),
            msg_id=str(row["msg_id"] or ""),
            error=detail,
        )
        return MessageReceipt(
            state="verifying",
            recipient_name=friend.agent_name,
            recipient_id=friend.agent_id,
            idempotency_key=key,
            msg_id=str(row["msg_id"] or ""),
            conv_id=str(row["conv_id"] or ""),
            duplicate=True,
            detail=detail,
        )

    def _write_state(
        self,
        key: str,
        *,
        state: str,
        conv_id: str = "",
        msg_id: str = "",
        error: str = "",
    ) -> None:
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """
                    UPDATE verified_external_actions
                       SET state = ?, conv_id = ?, msg_id = ?, last_error = ?,
                           updated_epoch = ?
                     WHERE idempotency_key = ?
                    """,
                    (state, conv_id, msg_id, error[:300], self.now(), key),
                )
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                # A duplicate receipt is not completion evidence. Keep the
                # action recoverable and let authoritative history find an
                # unclaimed receipt on a later reconciliation pass.
                db.execute(
                    """
                    UPDATE verified_external_actions
                       SET state='verifying',conv_id='',msg_id='',
                           last_error='receipt already belongs to another action',
                           updated_epoch=?
                     WHERE idempotency_key=?
                    """,
                    (self.now(), key),
                )
                db.commit()

    def _reserve(
        self,
        key: str,
        friend: Friend,
        payload_hash: str,
        contract_version: str,
    ) -> sqlite3.Row | None:
        now = self.now()
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    """
                    SELECT * FROM verified_external_actions
                     WHERE idempotency_key = ?
                    """,
                    (key,),
                ).fetchone()
                if existing is not None:
                    db.commit()
                    return existing
                db.execute(
                    """
                    INSERT INTO verified_external_actions(
                        idempotency_key, action_type, target_id, target_name,
                        payload_hash, contract_version, state, attempts,
                        created_epoch, updated_epoch
                    ) VALUES (?, 'eigenflux_message', ?, ?, ?, ?, 'attempting',
                              1, ?, ?)
                    """,
                    (
                        key,
                        friend.agent_id,
                        friend.agent_name,
                        payload_hash,
                        contract_version,
                        now,
                        now,
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return None

    def _start_retry(self, key: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                """
                UPDATE verified_external_actions
                   SET state = 'attempting', attempts = attempts + 1,
                       updated_epoch = ?, last_error = ''
                 WHERE idempotency_key = ?
                """,
                (self.now(), key),
            )

    def _verified_receipt(
        self,
        friend: Friend,
        key: str,
        conv_id: str,
        msg_id: str,
        *,
        duplicate: bool,
    ) -> MessageReceipt:
        if not self._claim_verified_receipt(key, conv_id, msg_id):
            return MessageReceipt(
                state="verifying",
                recipient_name=friend.agent_name,
                recipient_id=friend.agent_id,
                idempotency_key=key,
                duplicate=duplicate,
                detail="该服务端回执已被另一项动作认领，正在重新核验",
            )
        return MessageReceipt(
            state="verified",
            recipient_name=friend.agent_name,
            recipient_id=friend.agent_id,
            idempotency_key=key,
            msg_id=msg_id,
            conv_id=conv_id,
            duplicate=duplicate,
        )

    def send(
        self,
        recipient: str,
        content: str,
        *,
        repeat_token: str = "",
    ) -> MessageReceipt:
        message = str(content or "").strip()
        if not message:
            raise EigenFluxMessageError("消息正文为空，未发送")
        friend = self.resolve_friend(recipient)
        return self._send_friend(
            friend,
            message,
            repeat_token=repeat_token,
        )

    def send_to_friend_id(
        self,
        agent_id: str,
        content: str,
        *,
        repeat_token: str = "",
    ) -> MessageReceipt:
        """Send to one server-verified friend ID without label ambiguity."""
        wanted = str(agent_id or "").strip()
        matches = [
            friend for friend in self.list_friends()
            if friend.agent_id == wanted
        ]
        if len(matches) != 1:
            raise RecipientNotFound(
                "权威好友列表中没有唯一匹配的 agent ID，未发送"
            )
        message = str(content or "").strip()
        if not message:
            raise EigenFluxMessageError("消息正文为空，未发送")
        return self._send_friend(
            matches[0],
            message,
            repeat_token=repeat_token,
        )

    def _send_friend(
        self,
        friend: Friend,
        message: str,
        *,
        repeat_token: str,
    ) -> MessageReceipt:
        payload_hash = self._content_hash(message)
        contract_version = (
            f"repeat:{repeat_token.strip()}" if repeat_token.strip() else "v1"
        )
        key = self._action_key(
            friend.agent_id, payload_hash, contract_version
        )
        operation_started = self.now()
        existing = self._reserve(
            key, friend, payload_hash, contract_version
        )
        if existing is not None:
            state = str(existing["state"])
            if state == "verified":
                return MessageReceipt(
                    state="verified",
                    recipient_name=friend.agent_name,
                    recipient_id=friend.agent_id,
                    idempotency_key=key,
                    msg_id=str(existing["msg_id"]),
                    conv_id=str(existing["conv_id"]),
                    duplicate=True,
                )
            if state in {"attempting", "verifying"}:
                try:
                    found = self._find_verified_message(
                        friend,
                        message,
                        (
                            0
                            if str(existing["msg_id"] or "")
                            else float(existing["created_epoch"])
                            - self.verification_clock_skew_seconds
                        ),
                        conv_id=str(existing["conv_id"] or ""),
                        msg_id=str(existing["msg_id"] or ""),
                        action_key=key,
                    )
                except CliFailure as exc:
                    detail = _durable_failure(exc, "history readback")
                    self._write_state(key, state="verifying", error=detail)
                    return MessageReceipt(
                        state="verifying",
                        recipient_name=friend.agent_name,
                        recipient_id=friend.agent_id,
                        idempotency_key=key,
                        detail=detail,
                    )
                if found:
                    return self._verified_receipt(
                        friend, key, found[0], found[1], duplicate=True
                    )
                age = self.now() - float(existing["updated_epoch"])
                if age < self.attempt_stale_seconds:
                    return MessageReceipt(
                        state="attempting",
                        recipient_name=friend.agent_name,
                        recipient_id=friend.agent_id,
                        idempotency_key=key,
                    )
                return MessageReceipt(
                    state="verifying",
                    recipient_name=friend.agent_name,
                    recipient_id=friend.agent_id,
                    idempotency_key=key,
                    msg_id=str(existing["msg_id"] or ""),
                    conv_id=str(existing["conv_id"] or ""),
                    duplicate=True,
                    detail=(
                        str(existing["last_error"] or "")
                        or "权威历史尚未确认，未自动重复发送"
                    ),
                )

        try:
            response = self.api_sender(friend.agent_id, message)
        except Exception as raw_exc:
            exc = (
                raw_exc
                if isinstance(raw_exc, CliFailure)
                else CliFailure(
                    f"EigenFlux API send failed ({type(raw_exc).__name__})"
                )
            )
            detail = _durable_failure(exc, "send")
            self._write_state(key, state="verifying", error=detail)
            try:
                found = self._find_verified_message(
                    friend,
                    message,
                    operation_started - self.verification_clock_skew_seconds,
                    action_key=key,
                )
            except CliFailure:
                found = None
            if found:
                return self._verified_receipt(
                    friend, key, found[0], found[1], duplicate=False
                )
            return MessageReceipt(
                state="verifying",
                recipient_name=friend.agent_name,
                recipient_id=friend.agent_id,
                idempotency_key=key,
                detail=detail,
            )

        data = _response_data(response)
        msg_id = str(
            data.get("msg_id") or response.get("msg_id") or ""
        ).strip()
        conv_id = str(
            data.get("conv_id") or response.get("conv_id") or ""
        ).strip()
        self._write_state(
            key,
            state="verifying",
            conv_id=conv_id,
            msg_id=msg_id,
            error="" if msg_id and conv_id else "send response missing receipt IDs",
        )
        try:
            found = self._find_verified_message(
                friend,
                message,
                (
                    0
                    if msg_id
                    else operation_started
                    - self.verification_clock_skew_seconds
                ),
                conv_id=conv_id,
                msg_id=msg_id,
                action_key=key,
            )
        except CliFailure as exc:
            detail = _durable_failure(exc, "history readback")
            self._write_state(
                key,
                state="verifying",
                conv_id=conv_id,
                msg_id=msg_id,
                error=detail,
            )
            return MessageReceipt(
                state="verifying",
                recipient_name=friend.agent_name,
                recipient_id=friend.agent_id,
                idempotency_key=key,
                msg_id=msg_id,
                conv_id=conv_id,
                detail=detail,
            )
        if not found:
            detail = "权威历史中未找到目标一致的消息"
            self._write_state(
                key,
                state="verifying",
                conv_id=conv_id,
                msg_id=msg_id,
                error=detail,
            )
            return MessageReceipt(
                state="verifying",
                recipient_name=friend.agent_name,
                recipient_id=friend.agent_id,
                idempotency_key=key,
                msg_id=msg_id,
                conv_id=conv_id,
                detail=detail,
            )
        return self._verified_receipt(
            friend, key, found[0], found[1], duplicate=False
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a verified, idempotent EigenFlux friend message"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--recipient", required=True)
    resolve.add_argument("--format", choices=("text", "json"), default="text")
    send = sub.add_parser("send")
    send.add_argument("--recipient", required=True)
    body = send.add_mutually_exclusive_group(required=True)
    body.add_argument("--content")
    body.add_argument("--content-file")
    send.add_argument(
        "--repeat-token",
        default="",
        help="stable token only when the owner explicitly requests another copy",
    )
    send.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "resolve":
        try:
            friend = EigenFluxMessenger().resolve_friend(args.recipient)
        except EigenFluxMessageError as exc:
            if args.format == "json":
                print(json.dumps(
                    {"state": "rejected", "error": str(exc)},
                    ensure_ascii=False,
                ))
            else:
                print(f"❌ {exc}")
            return 2
        if args.format == "json":
            print(json.dumps(
                {
                    "state": "resolved",
                    "recipient_name": friend.agent_name,
                    "recipient_id": friend.agent_id,
                    "remark": friend.remark,
                },
                ensure_ascii=False,
            ))
        else:
            print(
                f"✅ 已绑定 {friend.agent_name}"
                f"（ID …{friend.agent_id[-8:]}）"
            )
        return 0
    try:
        content = (
            Path(args.content_file).read_text(encoding="utf-8")
            if args.content_file
            else args.content
        )
        receipt = EigenFluxMessenger().send(
            args.recipient,
            content,
            repeat_token=args.repeat_token,
        )
    except EigenFluxMessageError as exc:
        if args.format == "json":
            print(
                json.dumps(
                    {"state": "rejected", "error": str(exc)},
                    ensure_ascii=False,
                )
            )
        else:
            print(f"❌ {exc}")
        return 2
    if args.format == "json":
        print(json.dumps(asdict(receipt), ensure_ascii=False))
    else:
        print(receipt.human_text())
    return 0 if receipt.completed else 3


if __name__ == "__main__":
    sys.exit(main())
