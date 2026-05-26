"""Integration tests — verify the full heartbeat cycle runs end-to-end.

These test the REAL Python heartbeat loop (core/heartbeat_loop.py),
not mocked unit calls. They catch runtime errors like unbound variables,
import failures, and process lifecycle bugs that unit tests can't see.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def integration_env(tmp_path):
    """Set up a minimal Jarvis environment for integration testing."""
    # Create directory structure
    memory = tmp_path / "memory"
    for d in ["hot", "warm", "system", "timeline"]:
        (memory / d).mkdir(parents=True)

    # Minimal HEARTBEAT.md with one fast task
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("### test-task\n- interval: 5s\n- prompt: Say HEARTBEAT_OK\n")

    # Minimal heartbeat state
    (tmp_path / "heartbeat_state.json").write_text("{}")

    # Env vars
    env = os.environ.copy()
    env["JARVIS_DIR"] = str(tmp_path)
    env["MEMORY_DIR"] = str(memory)
    env["WORK_DIR"] = str(tmp_path)
    env["CHECK_INTERVAL"] = "2"  # fast cycles for testing
    env["HEARTBEAT_MODEL"] = "haiku"
    env["USER_ID"] = ""  # no Lark (headless mode)
    env["PYTHONPATH"] = str(REPO)

    return {"root": tmp_path, "memory": memory, "env": env}


def test_heartbeat_loop_starts_and_runs(integration_env):
    """The Python heartbeat loop should start, run ≥2 cycles, and not crash."""
    env = integration_env["env"]

    # Run in tmp dir to avoid touching repo files
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.heartbeat_loop"],
        env=env,
        cwd=str(integration_env["root"]),
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )

    # Wait for a few cycles (2s interval × 3 cycles = 6s + startup)
    time.sleep(10)

    # Process should still be alive
    assert proc.poll() is None, f"heartbeat_loop crashed (exit code {proc.returncode})"

    # Kill and collect stderr
    proc.send_signal(signal.SIGTERM)
    _, stderr = proc.communicate(timeout=5)
    output = stderr.decode(errors="replace")

    # Should have "Beat sent" markers
    beat_count = output.count("Beat sent")
    assert beat_count >= 2, f"Expected ≥2 beats, got {beat_count}. Output:\n{output[:500]}"


def test_heartbeat_loop_no_crash_on_missing_files(integration_env):
    """Loop should handle missing files gracefully, not crash."""
    env = integration_env["env"]
    # Delete heartbeat state to simulate fresh start
    (Path(env["JARVIS_DIR"]) / "heartbeat_state.json").unlink(missing_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "-m", "core.heartbeat_loop"],
        env=env,
        cwd=str(integration_env["root"]),
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )

    time.sleep(6)
    assert proc.poll() is None, "Crashed on missing state file"
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=5)


def test_heartbeat_state_updated_after_cycle(integration_env):
    """After running cycles, heartbeat_state.json should have last_run timestamps."""
    env = integration_env["env"]
    state_file = Path(env["JARVIS_DIR"]) / "heartbeat_state.json"

    proc = subprocess.Popen(
        [sys.executable, "-m", "core.heartbeat_loop"],
        env=env,
        cwd=str(integration_env["root"]),
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )

    time.sleep(8)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=5)

    state = json.loads(state_file.read_text())
    assert "test-task" in state, f"test-task not in state: {state}"
    assert state["test-task"]["last_run"] > 0


def test_bot_sh_syntax():
    """bot.sh must pass bash syntax check."""
    r = subprocess.run(["bash", "-n", str(REPO / "bot.sh")], capture_output=True)
    assert r.returncode == 0, f"bot.sh syntax error: {r.stderr.decode()}"


def test_bot_sh_shellcheck():
    """bot.sh must pass ShellCheck (if installed)."""
    if subprocess.run(["which", "shellcheck"], capture_output=True).returncode != 0:
        pytest.skip("shellcheck not installed")
    r = subprocess.run(
        ["shellcheck", "-s", "bash", "-S", "error", "-e", "SC1090,SC1091",
         str(REPO / "bot.sh")],
        capture_output=True,
    )
    assert r.returncode == 0, f"ShellCheck errors: {r.stdout.decode()}"
