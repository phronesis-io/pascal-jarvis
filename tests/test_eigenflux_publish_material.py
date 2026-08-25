from __future__ import annotations

import json
import subprocess
import sys

from core.eigenflux_publish_material import gate_material, material_digest


def test_empty_material_never_opens_gate_or_writes_state(tmp_path):
    state = tmp_path / "gate.json"
    decision = gate_material("  \n", state_path=state, now=100)
    assert decision.allowed is False
    assert decision.reason == "no_material"
    assert not state.exists()


def test_same_material_is_bounded_but_new_material_runs_immediately(tmp_path):
    state = tmp_path / "gate.json"
    first = gate_material("alpha\n", state_path=state, now=100)
    repeated = gate_material("alpha  \n\n", state_path=state, now=101)
    changed = gate_material("alpha\nbeta\n", state_path=state, now=102)

    assert first.allowed is True
    assert repeated.allowed is False
    assert repeated.reason == "unchanged_material"
    assert changed.allowed is True
    assert changed.reason == "new_material"


def test_unchanged_material_can_retry_after_window_without_storing_text(tmp_path):
    state = tmp_path / "gate.json"
    gate_material("private candidate text", state_path=state, now=100)
    decision = gate_material(
        "private candidate text", state_path=state, now=161, retry_seconds=60
    )

    payload = json.loads(state.read_text(encoding="utf-8"))
    assert decision.allowed is True
    assert decision.reason == "retry_window_elapsed"
    assert payload["last_attempt_digest"] == material_digest("private candidate text")
    assert "private" not in state.read_text(encoding="utf-8")
    assert state.stat().st_mode & 0o777 == 0o600


def test_publish_material_cli_accepts_stdin_without_persisting_it(tmp_path):
    state = tmp_path / "publish-gate.json"
    result = subprocess.run(
        [sys.executable, "-m", "core.eigenflux_publish_material",
         "--state", str(state), "--now", "100"],
        input="private draft material",
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "allow"
    assert "private draft" not in state.read_text(encoding="utf-8")
