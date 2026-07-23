import subprocess

import pytest

from core.actions import ActionProcessor
from core import eigenflux_friends


def _processor(tmp_path):
    return ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
    )


def test_card_action_accepts_and_sends_fixed_welcome(monkeypatch, tmp_path):
    calls = []
    results = [
        subprocess.CompletedProcess([], 0, stdout="accepted", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="sent", stderr=""),
    ]

    def fake_run(cmd):
        calls.append(cmd)
        return results.pop(0)

    monkeypatch.setattr(eigenflux_friends, "run_cli", fake_run)
    result = _processor(tmp_path)._do_eigenflux_friend(
        "request_id=123|decision=accept|from_uid=456|"
        "from_name=金融 Agent|remark=金融研究")

    assert "已通过" in result
    assert calls[0][:4] == [
        "eigenflux", "relation", "handle", "--request-id"]
    assert calls[1][:4] == [
        "eigenflux", "msg", "send", "--receiver-id"]
    assert eigenflux_friends.WELCOME_MESSAGE in calls[1]


def test_card_action_raises_when_server_does_not_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr(
        eigenflux_friends,
        "run_cli",
        lambda cmd: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="request is not pending"),
    )

    with pytest.raises(RuntimeError, match="服务端没有确认成功"):
        _processor(tmp_path)._do_eigenflux_friend(
            "request_id=123|decision=accept|from_uid=456")
