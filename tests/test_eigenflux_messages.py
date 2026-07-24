"""Regression tests for verified EigenFlux friend messages."""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from core.actions import ActionProcessor
from core.eigenflux_messages import (
    EigenFluxMessenger,
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
        self.send_count = 0
        self.send_response_has_ids = True
        self.history_error = False
        self.send_error_after_commit = False

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
            return self._result({"friends": self.friends})
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
        if command[1:3] == ["msg", "send"]:
            self.send_count += 1
            target = self._value(command, "--receiver-id")
            content = self._value(command, "--content")
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
                return self._result({}, returncode=1, stderr="connection reset")
            data = {"msg_id": msg_id, "conv_id": conv_id}
            if not self.send_response_has_ids:
                data = {}
            return self._result({"code": 0, "data": data})
        raise AssertionError(f"unexpected command: {command}")


def _messenger(tmp_path, cli: FakeEigenFlux, **kwargs) -> EigenFluxMessenger:
    return EigenFluxMessenger(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        runner=cli,
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
    send = next(call for call in cli.calls if call[1:3] == ["msg", "send"])
    assert send[send.index("--receiver-id") + 1] == "agent-spouse"
    assert any(call[1:3] == ["msg", "history"] for call in cli.calls)


def test_exact_remark_is_accepted(tmp_path):
    cli = FakeEigenFlux()
    receipt = _messenger(tmp_path, cli).send("Family agent", "hello")
    assert receipt.completed
    assert receipt.recipient_name == "Family Research Agent"


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


def test_readback_failure_never_claims_completion_or_retries(tmp_path):
    cli = FakeEigenFlux()
    cli.history_error = True
    receipt = _messenger(tmp_path, cli).send("Family agent", "do not assume")

    assert receipt.state == "verifying"
    assert not receipt.completed
    assert cli.send_count == 1
    assert "仍在核验" in receipt.human_text()


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
