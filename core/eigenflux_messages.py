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
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


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
    return str(
        data.get("next_cursor")
        or data.get("nextCursor")
        or obj.get("next_cursor")
        or obj.get("nextCursor")
        or ""
    ).strip()


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
        self.home = Path(
            home or os.environ.get("EIGENFLUX_HOME") or Path.home() / ".eigenflux"
        )
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
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except OSError:
                detail = ""
            raise CliFailure(
                f"EigenFlux API returned HTTP {exc.code}: {detail[:160]}"
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
            raise CliFailure(str(obj.get("msg") or f"API code {code}")[:300])
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
            db.commit()
            return db
        except Exception:
            db.close()
            raise

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
    ) -> tuple[str, str] | None:
        return self._find_verified_hash(
            friend.agent_id,
            self._content_hash(content),
            created_after,
            conv_id=conv_id,
            msg_id=msg_id,
        )

    def _find_verified_hash(
        self,
        target_id: str,
        payload_hash: str,
        created_after: float,
        *,
        conv_id: str = "",
        msg_id: str = "",
    ) -> tuple[str, str] | None:
        conversations = [conv_id] if conv_id else self._candidate_conversations(
            target_id
        )
        for candidate_conv in conversations:
            for row in self._history_messages(candidate_conv):
                row_msg_id = str(row.get("msg_id") or "").strip()
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
                return candidate_conv, row_msg_id
        return None

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
        with closing(self._connect()) as db, db:
            db.execute(
                """
                UPDATE verified_external_actions
                   SET state = ?, conv_id = ?, msg_id = ?, last_error = ?,
                       updated_epoch = ?
                 WHERE idempotency_key = ?
                """,
                (state, conv_id, msg_id, error[:300], self.now(), key),
            )

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
        self._write_state(
            key, state="verified", conv_id=conv_id, msg_id=msg_id
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
                    )
                except CliFailure as exc:
                    self._write_state(key, state="verifying", error=str(exc))
                    return MessageReceipt(
                        state="verifying",
                        recipient_name=friend.agent_name,
                        recipient_id=friend.agent_id,
                        idempotency_key=key,
                        detail=str(exc),
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
            self._start_retry(key)

        try:
            response = self.api_sender(friend.agent_id, message)
        except Exception as raw_exc:
            exc = (
                raw_exc
                if isinstance(raw_exc, CliFailure)
                else CliFailure(f"EigenFlux API send failed: {raw_exc}")
            )
            self._write_state(key, state="verifying", error=str(exc))
            try:
                found = self._find_verified_message(
                    friend,
                    message,
                    operation_started - self.verification_clock_skew_seconds,
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
                detail=str(exc),
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
            )
        except CliFailure as exc:
            self._write_state(
                key,
                state="verifying",
                conv_id=conv_id,
                msg_id=msg_id,
                error=str(exc),
            )
            return MessageReceipt(
                state="verifying",
                recipient_name=friend.agent_name,
                recipient_id=friend.agent_id,
                idempotency_key=key,
                msg_id=msg_id,
                conv_id=conv_id,
                detail=str(exc),
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
