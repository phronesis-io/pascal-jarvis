"""Recurring companion contact requires an explicit private subscription."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from core import retained_rhythms


ROOT = Path(__file__).resolve().parent.parent


def _write(root: Path, body: str) -> None:
    (root / "jarvis.yaml").write_text(body, encoding="utf-8")


def test_default_is_quiet(tmp_path):
    assert retained_rhythms.configured(tmp_path) == {
        "checkin": False,
        "daily_reflect": False,
        "exercise_week": False,
    }


def test_only_exact_boolean_true_is_an_explicit_subscription(tmp_path):
    _write(tmp_path, "retained_rhythms:\n  checkin: 'true'\n")
    assert retained_rhythms.is_enabled("checkin", tmp_path) is False
    _write(tmp_path, "retained_rhythms:\n  checkin: true\n")
    assert retained_rhythms.is_enabled("checkin", tmp_path) is True


def test_aliases_match_task_names(tmp_path):
    _write(tmp_path, (
        "retained_rhythms:\n"
        "  daily_reflect: true\n"
        "  exercise_week: true\n"
    ))
    assert retained_rhythms.is_enabled("daily-reflect", tmp_path)
    assert retained_rhythms.is_enabled("exercise-week", tmp_path)


def test_more_than_two_rhythms_fails_closed(tmp_path):
    _write(tmp_path, (
        "retained_rhythms:\n"
        "  checkin: true\n"
        "  daily_reflect: true\n"
        "  exercise_week: true\n"
    ))
    assert retained_rhythms.validation_error(tmp_path)
    assert not any(
        retained_rhythms.is_enabled(name, tmp_path)
        for name in retained_rhythms.RHYTHMS
    )


def test_disabled_post_hooks_emit_nothing_and_write_nothing(tmp_path):
    env = {
        **os.environ,
        "JARVIS_DIR": str(tmp_path),
        "MEMORY_DIR": str(tmp_path / "memory"),
        "PYTHONPATH": str(ROOT),
        "USER_ID": "",
    }
    for script in (
        "tasks/checkin_post.py",
        "tasks/daily_reflect_post.py",
        "tasks/exercise_week_post.py",
    ):
        result = subprocess.run(
            [sys.executable, str(ROOT / script)],
            input="a model generated message",
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
    assert not (tmp_path / "data").exists()


def test_pre_hooks_gate_before_network_model_or_harvest_work():
    for name in ("checkin", "daily_reflect", "exercise_week"):
        script = (ROOT / "tasks" / f"{name}_pre.sh").read_text(
            encoding="utf-8"
        )
        gate = script.index("core.retained_rhythms")
        assert gate < script.index("core.companion preflight") \
            if name == "checkin" else gate < script.index("date ")
        if name == "exercise_week":
            assert gate < script.index("core.lifelog")
