"""Regression tests for verified EigenFlux friend messages."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sqlite3
import subprocess
import threading
import urllib.error

import pytest

from core.actions import ActionProcessor
from core.eigenflux_messages import (
    CliFailure,
    EigenFluxApiClient,
    EigenFluxMessenger,
    MessageReceipt,
    RecipientAmbiguous,
    RecipientNotFound,
)


class FakeEigenFlux:
    def __init__(self):
        self.friends = [
            {
                "agent_id": "agent-spouse",
                "agent_name": "Family Research Agent",
                "remark": "Family agent",
            },
            {
                "agent_id": "agent-product",
                "agent_name": "Product Agent",
                "remark": "Product lead",
            },
        ]
        self.messages: list[dict] = []
        self.calls: list[list[str]] = []
        self.api_calls: list[tuple[str, str]] = []
        self.send_count = 0
        self.send_response_has_ids = True
        self.history_error = False
        self.send_error_after_commit = False
        self.friend_pages: list[list[dict]] | None = None

    @staticmethod
    def _value(command: list[str], flag: str) -> str:
        return command[command.index(flag) + 1]

    @staticmethod
    def _result(payload: dict, returncode: int = 0, stderr: str = ""):
        return subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=json.dumps(payload),
            stderr=stderr,
        )

    def __call__(self, command: list[str], **_kwargs):
        self.calls.append(command)
        if command[1:3] == ["relation", "friends"]:
            if self.friend_pages is None:
                return self._result({"friends": self.friends})
            cursor = (
                self._value(command, "--cursor")
                if "--cursor" in command else "0"
            )
            index = int(cursor)
            payload = {"friends": self.friend_pages[index]}
            if index + 1 < len(self.friend_pages):
                payload["next_cursor"] = str(index + 1)
            return self._result(payload)
        if command[1:3] == ["msg", "conversations"]:
            target_ids = {
                str(message.get("receiver_id") or "")
                for message in self.messages
            }
            conversations = [
                {
                    "conv_id": f"conv-{target}",
                    "participant_a": "agent-owner",
                    "participant_b": target,
                }
                for target in target_ids
                if target
            ]
            return self._result({"conversations": conversations})
        if command[1:3] == ["msg", "history"]:
            if self.history_error:
                return self._result({}, returncode=1, stderr="offline")
            conv_id = self._value(command, "--conv-id")
            messages = [
                message
                for message in self.messages
                if message["conv_id"] == conv_id
            ]
            return self._result({"messages": messages})
        raise AssertionError(f"unexpected command: {command}")

    def send_api(self, target: str, content: str) -> dict:
        self.api_calls.append((target, content))
        self.send_count += 1
        msg_id = f"msg-{self.send_count}"
        conv_id = f"conv-{target}"
        self.messages.insert(
            0,
            {
                "content": content,
                "conv_id": conv_id,
                "created_at": 2_000_000_000_000,
                "msg_id": msg_id,
                "receiver_id": target,
                "sender_id": "agent-owner",
            },
        )
        if self.send_error_after_commit:
            raise CliFailure("connection reset")
        data = {"msg_id": msg_id, "conv_id": conv_id}
        if not self.send_response_has_ids:
            data = {}
        return {"code": 0, "data": data}


def _messenger(tmp_path, cli: FakeEigenFlux, **kwargs) -> EigenFluxMessenger:
    return EigenFluxMessenger(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        runner=cli,
        api_sender=cli.send_api,
        now=lambda: 2_000_000_000,
        **kwargs,
    )


def test_exact_name_is_resolved_server_side_and_read_back(tmp_path):
    cli = FakeEigenFlux()
    receipt = _messenger(tmp_path, cli).send(
        "Family Research Agent", "D&O insurance brief"
    )

    assert receipt.completed
    assert receipt.recipient_id == "agent-spouse"
    assert receipt.msg_id == "msg-1"
    assert cli.api_calls == [("agent-spouse", "D&O insurance brief")]
    assert all("D&O insurance brief" not in arg for call in cli.calls for arg in call)
    assert any(call[1:3] == ["msg", "history"] for call in cli.calls)


def test_exact_remark_is_accepted(tmp_path):
    cli = FakeEigenFlux()
    receipt = _messenger(tmp_path, cli).send("Family agent", "hello")
    assert receipt.completed
    assert receipt.recipient_name == "Family Research Agent"


def test_friend_resolution_follows_all_cursor_pages(tmp_path):
    cli = FakeEigenFlux()
    cli.friend_pages = [
        [cli.friends[1]],
        [cli.friends[0]],
    ]

    receipt = _messenger(tmp_path, cli).send("Family agent", "hello")

    assert receipt.completed
    friend_calls = [
        call for call in cli.calls if call[1:3] == ["relation", "friends"]
    ]
    assert len(friend_calls) == 2
    assert "--cursor" in friend_calls[1]


def test_repeated_pagination_cursor_fails_closed(tmp_path):
    def looping_runner(command, **_kwargs):
        assert command[1:3] == ["relation", "friends"]
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({
                "friends": [],
                "next_cursor": "same-cursor",
            }),
            stderr="",
        )

    messenger = EigenFluxMessenger(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        runner=looping_runner,
    )

    with pytest.raises(CliFailure, match="cursor repeated"):
        messenger.list_friends()


def test_numeric_model_supplied_id_is_rejected_before_cli_call(tmp_path):
    cli = FakeEigenFlux()
    with pytest.raises(RecipientNotFound, match="数字 agent ID"):
        _messenger(tmp_path, cli).send("123456", "hello")
    assert cli.calls == []


def test_ambiguous_exact_remark_refuses_to_send(tmp_path):
    cli = FakeEigenFlux()
    cli.friends[0]["remark"] = "Shared alias"
    cli.friends[1]["remark"] = "Shared alias"
    with pytest.raises(RecipientAmbiguous):
        _messenger(tmp_path, cli).send("Shared alias", "hello")
    assert cli.send_count == 0


def test_verified_friend_id_bypasses_duplicate_display_names(tmp_path):
    cli = FakeEigenFlux()
    cli.friends[1]["agent_name"] = cli.friends[0]["agent_name"]
    messenger = _messenger(tmp_path, cli)

    receipt = messenger.send_to_friend_id("agent-spouse", "welcome")

    assert receipt.completed
    assert receipt.recipient_id == "agent-spouse"
    assert cli.api_calls == [("agent-spouse", "welcome")]


def test_binding_alias_is_checked_against_live_friend_record(tmp_path):
    cli = FakeEigenFlux()
    binding = tmp_path / "bindings.json"
    binding.write_text(
        json.dumps(
            {
                "bindings": {
                    "family": {
                        "agent_id": "agent-spouse",
                        "agent_name": "Family Research Agent",
                    }
                }
            }
        )
    )
    messenger = EigenFluxMessenger(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        bindings_path=binding,
        runner=cli,
        api_sender=cli.send_api,
        now=lambda: 2_000_000_000,
    )
    assert messenger.send("family", "hello").recipient_id == "agent-spouse"


def test_same_target_and_payload_is_idempotent(tmp_path):
    cli = FakeEigenFlux()
    messenger = _messenger(tmp_path, cli)
    first = messenger.send("Family agent", "same brief")
    second = messenger.send("Family agent", "same brief")

    assert first.completed and second.completed
    assert second.duplicate
    assert first.msg_id == second.msg_id
    assert cli.send_count == 1


def test_explicit_repeat_token_creates_a_new_contract(tmp_path):
    cli = FakeEigenFlux()
    messenger = _messenger(tmp_path, cli)
    first = messenger.send("Family agent", "same brief")
    repeated = messenger.send(
        "Family agent", "same brief", repeat_token="owner-request-2"
    )

    assert first.completed and repeated.completed
    assert repeated.msg_id != first.msg_id
    assert cli.send_count == 2


def test_uncertain_explicit_repeat_cannot_reuse_first_message_receipt(tmp_path):
    cli = FakeEigenFlux()
    messenger = _messenger(tmp_path, cli)
    first = messenger.send("Family agent", "same brief")
    cli.history_error = True

    def fail_before_commit(_target, _content):
        raise CliFailure("connection reset before commit")

    messenger.api_sender = fail_before_commit
    repeated = messenger.send(
        "Family agent", "same brief", repeat_token="owner-request-2"
    )
    assert repeated.state == "verifying"

    cli.history_error = False
    reconciled = messenger.reconcile_action(repeated.idempotency_key)

    assert reconciled.state == "verifying"
    assert reconciled.msg_id == ""
    assert reconciled.idempotency_key != first.idempotency_key


def test_concurrent_explicit_repeats_claim_distinct_receipts_atomically(tmp_path):
    cli = FakeEigenFlux()
    cli.send_response_has_ids = False
    barrier = threading.Barrier(2)
    send_lock = threading.Lock()

    def send_api(target, content):
        with send_lock:
            return cli.send_api(target, content)

    class RacingMessenger(EigenFluxMessenger):
        def _candidate_conversations(self, target_id):
            barrier.wait(timeout=2)
            return super()._candidate_conversations(target_id)

    messenger = RacingMessenger(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        runner=cli,
        api_sender=send_api,
        now=lambda: 2_000_000_000,
    )
    messenger._connect().close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda token: messenger.send(
                    "Family agent",
                    "same brief",
                    repeat_token=token,
                ),
                ("owner-repeat-1", "owner-repeat-2"),
            )
        )

    assert all(receipt.completed for receipt in receipts)
    assert {receipt.msg_id for receipt in receipts} == {"msg-1", "msg-2"}
    with sqlite3.connect(tmp_path / "jarvis.db") as db:
        claimed, distinct = db.execute(
            """
            SELECT COUNT(msg_id), COUNT(DISTINCT msg_id)
              FROM verified_external_actions WHERE msg_id != ''
            """
        ).fetchone()
    assert claimed == distinct == 2


def test_legacy_duplicate_receipts_are_reopened_before_unique_index(tmp_path):
    db_path = tmp_path / "jarvis.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE verified_external_actions (
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
            """
        )
        values = (
            "eigenflux_message",
            "agent-spouse",
            "Family Research Agent",
            "payload-hash",
            "v1",
            "verified",
            1,
            "conv-agent-spouse",
            "duplicate-message",
            1.0,
            1.0,
            "",
        )
        db.execute(
            "INSERT INTO verified_external_actions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("action-1", *values),
        )
        db.execute(
            "INSERT INTO verified_external_actions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("action-2", *values),
        )

    messenger = EigenFluxMessenger(
        root=tmp_path,
        db_path=db_path,
        runner=lambda *_args, **_kwargs: None,
    )
    messenger._connect().close()

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            """
            SELECT idempotency_key,state,msg_id,last_error
              FROM verified_external_actions ORDER BY idempotency_key
            """
        ).fetchall()
        unique_index = db.execute(
            """
            SELECT 1 FROM sqlite_master
             WHERE type='index'
               AND name='idx_verified_actions_unique_message_receipt'
            """
        ).fetchone()
    assert rows[0][1:3] == ("verified", "duplicate-message")
    assert rows[1][1:3] == ("verifying", "")
    assert rows[1][3] == "duplicate receipt claim released"
    assert unique_index == (1,)


def test_missing_send_receipt_can_only_complete_from_history(tmp_path):
    cli = FakeEigenFlux()
    cli.send_response_has_ids = False
    receipt = _messenger(tmp_path, cli).send("Family agent", "recover me")

    assert receipt.completed
    assert receipt.msg_id == "msg-1"
    assert receipt.conv_id == "conv-agent-spouse"


def test_connection_error_after_server_commit_reconciles_without_resend(tmp_path):
    cli = FakeEigenFlux()
    cli.send_error_after_commit = True
    receipt = _messenger(tmp_path, cli).send("Family agent", "recover me")

    assert receipt.completed
    assert cli.send_count == 1


def test_exact_receipt_ids_override_clock_skew(tmp_path):
    cli = FakeEigenFlux()
    messenger = EigenFluxMessenger(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        runner=cli,
        api_sender=cli.send_api,
        now=lambda: 9_000_000_000,
    )

    receipt = messenger.send("Family agent", "clock-skewed receipt")

    assert receipt.completed
    assert receipt.msg_id == "msg-1"


def test_readback_failure_never_claims_completion_or_retries(tmp_path):
    cli = FakeEigenFlux()
    cli.history_error = True
    receipt = _messenger(tmp_path, cli).send("Family agent", "do not assume")

    assert receipt.state == "verifying"
    assert not receipt.completed
    assert cli.send_count == 1
    assert "仍在核验" in receipt.human_text()


def test_stale_uncertain_action_never_replays_without_repeat_token(tmp_path):
    cli = FakeEigenFlux()
    clock = [2_000_000_000.0]
    sends = []

    def uncertain_send(target, content):
        sends.append((target, content))
        return {
            "code": 0,
            "data": {"msg_id": "unknown-1", "conv_id": "conv-agent-spouse"},
        }

    messenger = EigenFluxMessenger(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        runner=cli,
        api_sender=uncertain_send,
        now=lambda: clock[0],
        attempt_stale_seconds=60,
    )

    first = messenger.send("Family agent", "uncertain")
    clock[0] += 61
    second = messenger.send("Family agent", "uncertain")

    assert first.state == "verifying"
    assert second.state == "verifying"
    assert second.duplicate is True
    assert sends == [("agent-spouse", "uncertain")]


def test_uncertain_action_projects_stable_key_for_later_reconciliation(
    monkeypatch, tmp_path,
):
    from core.delegations import DelegationStore

    cli = FakeEigenFlux()
    cli.history_error = True
    messenger = _messenger(tmp_path, cli)
    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxMessenger",
        lambda **_kwargs: messenger,
    )
    processor = ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
    )
    encoded = base64.b64encode(b"recover later").decode()

    result = processor._do_eigenflux_message(
        f"recipient=Family agent|content_b64={encoded}"
    )

    assert "仍在核验" in result
    detail = DelegationStore(root=tmp_path).get(
        DelegationStore(root=tmp_path).list()[0]["id"]
    )
    assert detail["status"] == "verifying"
    assert detail["verification_policy"]["idempotency_key"]
    from core.delegation_verify import VerifierRegistry

    cli.history_error = False
    verification = VerifierRegistry(
        root=tmp_path, db_path=tmp_path / "jarvis.db", runner=cli
    ).verify(
        "eigenflux_message",
        detail["expected_postcondition"],
        detail["verification_policy"],
    )
    assert verification.matched is True
    assert verification.observed_summary.find('"state":"verified"') >= 0
    assert cli.send_count == 1


def test_reconciler_projects_an_unclaimed_message_receipt(
    tmp_path,
):
    from core.delegation_reconcile import DelegationReconciler
    from core.delegations import DelegationStore

    cli = FakeEigenFlux()
    cli.history_error = True
    receipt = _messenger(tmp_path, cli).send(
        "Family agent", "welcome needs recovery"
    )
    assert receipt.state == "verifying"
    store = DelegationStore(root=tmp_path, db_path=tmp_path / "jarvis.db")
    assert store.list() == []

    result = DelegationReconciler(store=store).run(send_items=False)

    assert result["connector_projections_repaired"] == 1
    detail = store.get(store.list()[0]["id"])
    assert detail["status"] == "verifying"
    assert (
        detail["verification_policy"]["idempotency_key"]
        == receipt.idempotency_key
    )


def test_uncertain_explicit_repeats_keep_separate_delegations(
    monkeypatch, tmp_path,
):
    from core.delegations import DelegationStore

    keys = iter(("action-key-one", "action-key-two"))

    class FakeMessenger:
        def __init__(self, **_kwargs):
            pass

        def send(self, _recipient, _content, repeat_token=""):
            return MessageReceipt(
                state="verifying",
                recipient_name="Family Research Agent",
                recipient_id="agent-spouse",
                idempotency_key=next(keys),
            )

    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxMessenger", FakeMessenger
    )
    processor = ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
    )
    encoded = base64.b64encode(b"same uncertain brief").decode()

    processor._do_eigenflux_message(
        f"recipient=Family agent|content_b64={encoded}"
    )
    processor._do_eigenflux_message(
        f"recipient=Family agent|content_b64={encoded}"
        "|repeat_token=owner-request-2"
    )

    rows = DelegationStore(root=tmp_path).list(limit=10)
    assert len(rows) == 2
    assert {row["source_ref"] for row in rows} == {
        "attempt:action-key-one",
        "attempt:action-key-two",
    }


class _ApiResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_api_sender_keeps_message_body_out_of_url_and_headers(tmp_path):
    home = tmp_path / ".eigenflux"
    credentials = home / "servers" / "production" / "credentials.json"
    credentials.parent.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({
        "default_server": "production",
        "servers": [{
            "name": "production",
            "endpoint": "https://eigenflux.example.test",
        }],
    }))
    credentials.write_text(json.dumps({
        "access_token": "private-token",
        "agent_id": "owner",
    }))
    captured = {}

    def open_request(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _ApiResponse({
            "code": 0,
            "data": {"msg_id": "m1", "conv_id": "c1"},
        })

    response = EigenFluxApiClient(home, opener=open_request).send(
        "agent-spouse", "private insurance brief"
    )

    request = captured["request"]
    assert request.full_url == "https://eigenflux.example.test/api/v1/pm/send"
    assert "private insurance brief" not in request.full_url
    assert all(
        "private insurance brief" not in str(value)
        for value in request.headers.values()
    )
    assert json.loads(request.data) == {
        "receiver_id": "agent-spouse",
        "content": "private insurance brief",
    }
    assert response["data"]["msg_id"] == "m1"


def test_api_sender_resolves_cli_home_suffix_from_environment(
    tmp_path, monkeypatch,
):
    base = tmp_path / "agent-home"
    monkeypatch.setenv("EIGENFLUX_HOME", str(base))

    client = EigenFluxApiClient()

    assert client.home == base / ".eigenflux"


def test_api_sender_does_not_duplicate_existing_home_suffix(tmp_path):
    home = tmp_path / ".eigenflux"

    assert EigenFluxApiClient(home).home == home


def test_http_error_body_is_never_persisted_as_message_state(tmp_path):
    home = tmp_path / ".eigenflux"
    credentials = home / "servers" / "production" / "credentials.json"
    credentials.parent.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({
        "default_server": "production",
        "servers": [{
            "name": "production",
            "endpoint": "https://eigenflux.example.test",
        }],
    }))
    credentials.write_text(json.dumps({
        "access_token": "private-token",
        "agent_id": "owner",
    }))
    private_body = "private insurance brief must not persist"

    def rejected(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            422,
            "validation failed",
            {},
            io.BytesIO(
                json.dumps({"error": private_body}).encode("utf-8")
            ),
        )

    cli = FakeEigenFlux()
    database = tmp_path / "jarvis.db"
    messenger = EigenFluxMessenger(
        root=tmp_path,
        db_path=database,
        runner=cli,
        api_sender=EigenFluxApiClient(home, opener=rejected).send,
        now=lambda: 2_000_000_000,
    )

    receipt = messenger.send("Family agent", private_body)

    assert receipt.state == "verifying"
    assert private_body not in receipt.detail
    with sqlite3.connect(database) as db:
        error = db.execute(
            "SELECT last_error FROM verified_external_actions"
        ).fetchone()[0]
    assert error == "EigenFlux send failed: HTTP 4xx"
    assert private_body not in error


def test_api_sender_fails_closed_without_credentials(tmp_path):
    with pytest.raises(CliFailure, match="authentication is not configured"):
        EigenFluxApiClient(tmp_path).send("agent", "content")


def test_action_marker_returns_deterministic_receipt(monkeypatch, tmp_path):
    class FakeMessenger:
        def __init__(self, **_kwargs):
            pass

        def send(self, recipient, content, repeat_token=""):
            assert recipient == "Family agent"
            assert content == "structured | body ] remains intact"
            assert repeat_token == ""

            class Receipt:
                @staticmethod
                def human_text():
                    return "✅ verified receipt"

            return Receipt()

    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxMessenger", FakeMessenger
    )
    encoded = base64.b64encode(
        "structured | body ] remains intact".encode()
    ).decode()
    processor = ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
    )
    output = processor.process(
        "已经发送成功。"
        f"[ACTION:eigenflux_message|recipient=Family agent|content_b64={encoded}]"
    )
    assert "[ACTION:" not in output
    assert "已经发送成功" not in output
    assert "✅ verified receipt" in output


def test_action_failure_suppresses_model_success_claim(monkeypatch, tmp_path):
    class FakeMessenger:
        def __init__(self, **_kwargs):
            pass

        def send(self, _recipient, _content, repeat_token=""):
            class Receipt:
                @staticmethod
                def human_text():
                    return "⚠️ 发送结果仍在核验，未自动重复发送。"

            return Receipt()

    monkeypatch.setattr(
        "core.eigenflux_messages.EigenFluxMessenger", FakeMessenger
    )
    encoded = base64.b64encode("brief".encode()).decode()
    processor = ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
    )
    output = processor.process(
        "已经发好了。"
        f"[ACTION:eigenflux_message|recipient=Family agent|content_b64={encoded}]"
    )
    assert "已经发好了" not in output
    assert output == "⚠️ 发送结果仍在核验，未自动重复发送。"
