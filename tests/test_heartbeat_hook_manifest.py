"""Every hook HEARTBEAT.md declares must actually be runnable.

A task whose pre-script cannot be executed fails as `pre_error` on every cycle
and trips its circuit — the task simply never runs, and the only symptom is a
counter in heartbeat_state.json. That is exactly what happened to `routine-run`
on 2026-07-28: the file was created without its executable bit (editors and
tooling write 0644), the resident bot's exec failed 5/5 times, and the circuit
opened before the feature had ever run once.

These checks are cheap and cover the whole class, for every task, forever.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.heartbeat import parse_heartbeat  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TASKS = parse_heartbeat(ROOT / "HEARTBEAT.md")


def _declared(kind: str) -> list[tuple[str, Path]]:
    return [(t["name"], ROOT / t[kind]) for t in TASKS if t.get(kind)]


def test_heartbeat_declares_tasks_at_all():
    assert len(TASKS) > 10, "parse_heartbeat returned suspiciously few tasks"


@pytest.mark.parametrize("name,path", _declared("pre"),
                         ids=[n for n, _ in _declared("pre")])
def test_pre_hook_exists_and_is_executable(name, path):
    assert path.is_file(), f"{name}: declared pre-hook {path} does not exist"
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"{name}: {path.name} is not executable — the heartbeat exec's it "
        f"directly, so every cycle fails as pre_error and trips the circuit. "
        f"Run: chmod +x {path.relative_to(ROOT)}")
    assert os.access(path, os.X_OK), f"{name}: {path.name} is not runnable"


@pytest.mark.parametrize("name,path", _declared("post"),
                         ids=[n for n, _ in _declared("post")])
def test_post_hook_exists(name, path):
    # Post-hooks are invoked as `python3 <path>` with the envelope on stdin,
    # so they do not need the executable bit — but they must exist.
    assert path.is_file(), f"{name}: declared post-hook {path} does not exist"


@pytest.mark.parametrize("name,path", _declared("pre"),
                         ids=[n for n, _ in _declared("pre")])
def test_shell_pre_hook_parses(name, path):
    import subprocess
    if path.suffix != ".sh":
        pytest.skip("not a shell script")
    result = subprocess.run(["bash", "-n", str(path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"{name}: {path.name} syntax error\n{result.stderr}"


def test_every_task_has_a_prompt_or_is_deterministic():
    """A task with neither a prompt nor a pre-hook can never do anything."""
    for task in TASKS:
        assert task.get("prompt") or task.get("pre"), (
            f"{task['name']}: has neither a prompt nor a pre-hook")
