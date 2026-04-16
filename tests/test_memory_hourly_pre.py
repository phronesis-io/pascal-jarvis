"""Regression test for Bug #2 — hourly pre-script needs CLAUDE_PROJECT_DIR.

If neither CLAUDE_PROJECT_DIR nor SESSION_DIR is a valid directory, the script
must exit 0 (silent skip). If it IS valid, the script should read session files.
"""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "memory_hourly_pre.sh"
NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def _run(env: dict) -> subprocess.CompletedProcess:
    clean_env = {**os.environ, **env}
    # Unset potentially-inherited values that would confuse the test
    clean_env.setdefault("PATH", os.environ.get("PATH", ""))
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env=clean_env,
    )


def test_no_session_dir_skips_cleanly(tmp_path):
    result = _run({
        "JARVIS_DIR": str(tmp_path),
        "CLAUDE_PROJECT_DIR": "",
        "SESSION_DIR": "",
    })
    assert result.returncode == 0
    assert result.stdout == ""


def test_session_dir_missing_dir_skips(tmp_path):
    result = _run({
        "JARVIS_DIR": str(tmp_path),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "nonexistent"),
    })
    assert result.returncode == 0
    assert result.stdout == ""


def test_reads_session_file_when_configured(tmp_path):
    """Write a synthetic session with a recent message and verify it's extracted."""
    jarvis = tmp_path / "jarvis"
    jarvis.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    conv_key = "ou_test"
    counter = 1
    sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-{counter}"))

    tracker = {conv_key: {"session_id": sid, "counter": counter}}
    (jarvis / "active_sessions.json").write_text(json.dumps(tracker))

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    session_lines = [
        json.dumps({
            "type": "user",
            "timestamp": now_iso,
            "message": {"role": "user", "content": "HELLO_MARKER"},
        }),
        json.dumps({
            "type": "assistant",
            "timestamp": now_iso,
            "message": {"role": "assistant", "content": "REPLY_MARKER"},
        }),
    ]
    (sessions / f"{sid}.jsonl").write_text("\n".join(session_lines) + "\n")

    result = _run({
        "JARVIS_DIR": str(jarvis),
        "CLAUDE_PROJECT_DIR": str(sessions),
    })
    assert result.returncode == 0
    assert "HELLO_MARKER" in result.stdout
    assert "REPLY_MARKER" in result.stdout
