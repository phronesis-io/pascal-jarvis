"""Tests for tasks/eigenflux_messages_post.py — the auto_reply_pm gate.

EigenFlux's `auto_reply_pm` switch (console-controllable, synced to local config
after each feed poll) lets the user turn OFF autonomous PM replies. This post
script is the single choke point where Jarvis replies to a PM *sender*, so the
gate must live here. Per the upstream contract, only an explicit `false`
suppresses replies; unset/`true` default to ON.

A fake `eigenflux` shim on PATH controls what `config get` returns (and makes
`msg send` a harmless no-op), so we can exercise the gate without the network.
"""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "eigenflux_messages_post.py"

REPLY_PAYLOAD = (
    '{"reply_actions":[{"receiver_id":"u1","content":"你好"}],'
    '"user_message":"有人给你发了消息"}'
)


def _fake_eigenflux(tmp_path: Path, auto_reply_value: str) -> Path:
    """A shim that answers `config get --key auto_reply_pm` with auto_reply_value
    (may be empty = unset), captures `msg send` argv to bin/../sent.json, and
    treats every other call as success."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "eigenflux"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, pathlib\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['config', 'get']:\n"
        f"    sys.stdout.write({auto_reply_value!r})\n"
        "    sys.exit(0)\n"
        "if a[:2] == ['msg', 'send']:\n"
        f"    pathlib.Path({str(tmp_path / 'sent.json')!r}).write_text(json.dumps(a))\n"
        "sys.exit(0)\n"
    )
    shim.chmod(0o755)
    return bindir


def _run(payload: str, tmp_path: Path, auto_reply_value: str):
    bindir = _fake_eigenflux(tmp_path, auto_reply_value)
    env = {
        "JARVIS_DIR": str(tmp_path),
        "PATH": f"{bindir}:/usr/bin:/bin",
    }
    r = subprocess.run([sys.executable, str(SCRIPT)], input=payload,
                       capture_output=True, text=True, env=env)
    return r  # .stdout / .returncode


def test_auto_reply_off_holds_replies(tmp_path):
    """auto_reply_pm=false: the reply is held and Pascal is told."""
    out = _run(REPLY_PAYLOAD, tmp_path, "false").stdout
    assert "auto_reply_pm 已关闭" in out
    assert "已暂停" in out
    assert "有人给你发了消息" in out  # original surface text still shown
    assert not (tmp_path / "sent.json").exists()  # nothing was actually sent


def test_auto_reply_on_does_not_hold(tmp_path):
    """auto_reply_pm=true: no hold note; the message is surfaced normally."""
    out = _run(REPLY_PAYLOAD, tmp_path, "true").stdout
    assert "auto_reply_pm 已关闭" not in out
    assert "有人给你发了消息" in out


def test_auto_reply_unset_defaults_on(tmp_path):
    """Unset (empty) defaults to ON — only an explicit false disables."""
    out = _run(REPLY_PAYLOAD, tmp_path, "").stdout
    assert "auto_reply_pm 已关闭" not in out


def test_heartbeat_ok_is_noop(tmp_path):
    out = _run("HEARTBEAT_OK", tmp_path, "false").stdout
    assert out.strip() == ""


def test_numeric_receiver_id_coerced_no_crash(tmp_path):
    """Regression: the LLM sometimes emits receiver_id as a JSON NUMBER. Before
    str()-coercion that raised an uncaught TypeError in subprocess.run (cmd must
    be all-str), crashing the whole hook. It must instead pass a string id."""
    payload = (
        '{"reply_actions":[{"receiver_id":789123456789,"content":"hi"}],'
        '"user_message":"surface"}'
    )
    r = _run(payload, tmp_path, "true")
    assert r.returncode == 0, f"hook crashed: {r.stderr}"
    sent = tmp_path / "sent.json"
    assert sent.exists(), "msg send was never invoked"
    argv = __import__("json").loads(sent.read_text())
    rid = argv[argv.index("--receiver-id") + 1]
    assert rid == "789123456789"  # passed as a string, not an int


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
