"""Regression tests for the repository write guard's process detection."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _guard_module():
    path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("jarvis_test_guard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_runtime_guard_reads_complete_process_arguments(monkeypatch):
    conftest = _guard_module()
    seen = []

    def fake_run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=("/opt/homebrew/very/long/python/path/python3 "
                    "-m core.heartbeat_loop\n"),
        )

    monkeypatch.setattr(conftest, "_SUBPROCESS_RUN", fake_run)

    assert conftest._bot_is_running() is True
    assert seen[0][0] == ["ps", "-eo", "args"]
    assert seen[0][1]["text"] is True


def test_live_runtime_guard_does_not_match_an_unrelated_python(monkeypatch):
    conftest = _guard_module()
    monkeypatch.setattr(
        conftest,
        "_SUBPROCESS_RUN",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="python3 -m core.components\n"),
    )

    assert conftest._bot_is_running() is False
