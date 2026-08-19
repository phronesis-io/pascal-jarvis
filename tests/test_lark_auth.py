"""Device-flow engine: the「现在授权」button's actual muscle.

Every path is exercised with an injected runner — no real lark-cli, no real
keychain, no real Feishu sends.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.lark_auth as lark_auth  # noqa: E402
from core import lark_bot_transport  # noqa: E402

FLOW_JSON = json.dumps({
    "verification_url": "https://accounts.feishu.cn/oauth/v1/device/verify?x=1",
    "device_code": "dc_test123", "expires_in": 600})

STATUS_READY = json.dumps({"identities": {"user": {"status": "ready"}}})
STATUS_MISSING = json.dumps({"identities": {"user": {"status": "missing"}}})


class Runner:
    """Records argv; answers by command prefix."""

    def __init__(self, answers):
        self.answers = answers  # list of (predicate, returncode, stdout)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        for pred, rc, out in self.answers:
            if pred(argv):
                return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        raise AssertionError(f"unexpected command: {argv}")


def _is_login_start(argv):
    return "auth" in argv and "--no-wait" in argv


def _is_dm(argv):
    return "+messages-send" in argv


def _is_poll(argv):
    return "--device-code" in argv


def _is_status(argv):
    return "status" in argv


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setenv("USER_ID", "ou_test")
    # Default every test to the injected CLI runner.  Without this isolation
    # the keychain-independent bot transport reads the production jarvis.yaml
    # and can send a real DM even though this suite promises no real Feishu
    # traffic.  The dedicated direct-transport test overrides this stub.
    monkeypatch.setattr(
        lark_bot_transport,
        "send",
        lambda **kwargs: lark_bot_transport.BotSendResult(False, False),
    )
    yield


def test_start_sends_link_and_detaches_poller():
    run = Runner([(_is_login_start, 0, FLOW_JSON), (_is_dm, 0, "{}")])
    spawned = []

    def popen(argv, **kwargs):
        spawned.append((argv, kwargs))
        return SimpleNamespace(pid=4242)

    receipt = lark_auth.start_device_flow(run=run, popen=popen)
    assert "授权链接" in receipt and "飞书" in receipt
    dm = next(argv for argv in run.calls if _is_dm(argv))
    assert "--as" in dm and "bot" in dm  # user identity is the thing that's broken
    assert "ou_test" in dm
    (argv, kwargs), = spawned
    assert argv[-3:] == ["core.lark_auth", "poll", "dc_test123"]
    assert kwargs.get("start_new_session") is True


def test_device_flow_receipt_prefers_keychain_independent_bot_api(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lark_bot_transport,
        "send",
        lambda **kwargs: (
            calls.append(kwargs)
            or lark_bot_transport.BotSendResult(True, True, "om_auth")
        ),
    )

    assert lark_auth._send_dm(
        "authorize",
        run=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI should not run")
        ),
    ) is True
    assert calls == [{
        "text": "authorize", "user_id": "ou_test", "root": lark_auth.JARVIS_DIR,
    }]


def test_start_failure_raises_never_claims_success():
    run = Runner([(_is_login_start, 1, "")])
    with pytest.raises(RuntimeError):
        lark_auth.start_device_flow(run=run, popen=lambda *a, **k: None)


def test_start_with_failed_dm_hands_the_link_back():
    run = Runner([(_is_login_start, 0, FLOW_JSON), (_is_dm, 1, "")])
    receipt = lark_auth.start_device_flow(
        run=run, popen=lambda *a, **k: None)
    assert "https://accounts.feishu.cn" in receipt


def test_poll_success_verifies_then_receipts(monkeypatch, tmp_path):
    monkeypatch.setattr(lark_auth, "TRIGGER_PATH", tmp_path / "trigger")
    run = Runner([(_is_poll, 0, "{}"), (_is_status, 0, STATUS_READY),
                  (_is_dm, 0, "{}")])
    assert lark_auth.poll("dc_test123", run=run) == 0
    assert any(_is_dm(argv) for argv in run.calls)
    assert (tmp_path / "trigger").exists()


def test_poll_does_not_receipt_on_unverified_token(monkeypatch, tmp_path):
    """Exit-0 from the CLI is not proof — 2026-08-07 the first poll exited 0
    while the token was still missing (code had expired)."""
    monkeypatch.setattr(lark_auth, "TRIGGER_PATH", tmp_path / "trigger")
    run = Runner([(_is_poll, 0, "{}"), (_is_status, 0, STATUS_MISSING)])
    assert lark_auth.poll("dc_test123", run=run) == 1
    assert not any(_is_dm(argv) for argv in run.calls)
    assert not (tmp_path / "trigger").exists()


def test_poll_timeout_is_a_quiet_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(lark_auth, "TRIGGER_PATH", tmp_path / "trigger")

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    assert lark_auth.poll("dc_test123", run=run) == 1


def test_json_parse_survives_noise_around_the_envelope():
    noisy = ('lark-cli 1.0.84 available, run: lark-cli update\n'
             '{"_notice": {"update": {"message": "..."}}}\n'
             + STATUS_READY + '\ntrailing prose')
    parsed = lark_auth._parse_json_output(noisy)
    assert parsed["identities"]["user"]["status"] == "ready"
    with pytest.raises(ValueError):
        lark_auth._parse_json_output("no json here at all")


def test_poller_spawn_failure_raises_before_the_link_is_promised():
    """DM'ing a link nobody polls would let him authorize into the void —
    the poller must be up before anything is promised."""
    run = Runner([(_is_login_start, 0, FLOW_JSON), (_is_dm, 0, "{}")])

    def popen(argv, **kwargs):
        raise OSError("spawn failed")

    with pytest.raises(OSError):
        lark_auth.start_device_flow(run=run, popen=popen)
    assert not any(_is_dm(argv) for argv in run.calls)
