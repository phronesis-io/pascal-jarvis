from __future__ import annotations

import json
import subprocess
import sys

from core.change_gate import allow_tree_change, tree_signature


def test_tree_gate_runs_on_change_and_once_on_new_local_day(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    (root / "note.md").write_text("alpha", encoding="utf-8")
    state = root / "system" / "gate.json"

    first = allow_tree_change(root, state_path=state, now=100)
    same = allow_tree_change(root, state_path=state, now=101)
    next_day = allow_tree_change(root, state_path=state, now=100 + 86400)
    (root / "note.md").write_text("beta", encoding="utf-8")
    changed = allow_tree_change(root, state_path=state, now=100 + 86401)

    assert first == (True, "changed")
    assert same == (False, "unchanged")
    assert next_day == (True, "daily_refresh")
    assert changed == (True, "changed")
    assert state.stat().st_mode & 0o777 == 0o600


def test_gate_state_does_not_invalidate_its_own_signature(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    (root / "note.md").write_text("alpha", encoding="utf-8")
    state = root / "system" / "gate.json"
    allow_tree_change(root, state_path=state, now=100)
    before = json.loads(state.read_text(encoding="utf-8"))["signature"]

    assert tree_signature(root, exclude=(state.name,)) == before
    assert allow_tree_change(root, state_path=state, now=101) == (False, "unchanged")


def test_change_gate_cli_reports_allow_then_skip(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    (root / "note.md").write_text("alpha", encoding="utf-8")
    state = tmp_path / "gate.json"
    command = [
        sys.executable, "-m", "core.change_gate", "tree",
        "--root", str(root), "--state", str(state), "--now", "100",
    ]

    first = subprocess.run(command, capture_output=True, text=True, check=True)
    second = subprocess.run(command, capture_output=True, text=True, check=True)

    assert first.stdout.strip() == "allow"
    assert second.stdout.strip() == "skip"
