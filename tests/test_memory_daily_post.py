"""Regression test for Bug #1 — memory_daily_post.py NameError when UPDATE present.

Before the fix this would crash and leave hourly_log unarchived + daily_log empty.
"""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "memory_daily_post.py"


def _run(stdin: str, memory_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "MEMORY_DIR": str(memory_dir)},
    )


def test_plain_summary_writes_daily_log(tmp_path):
    tl = tmp_path / "timeline"
    tl.mkdir()
    (tl / "hourly_log.md").write_text("### 10:00\nsome hourly entry\n")
    result = _run("- Morning check-in\n- Portfolio review", tmp_path)
    assert result.returncode == 0
    assert (tl / "daily_log.md").exists()
    assert "Morning check-in" in (tl / "daily_log.md").read_text()
    # Hourly should have been archived + cleared
    assert (tl / "hourly_archive.md").exists()
    assert (tl / "hourly_log.md").read_text() == ""


def test_summary_with_update_directives_does_not_crash(tmp_path):
    """Regression: previously _ensure_pending was called before definition → NameError."""
    stdin = (
        "- Normal day\n"
        "→ UPDATE: user_profile.md: add new role detail\n"
        "→ UPDATE: interaction_principles.md: tone adjustment\n"
    )
    result = _run(stdin, tmp_path)
    assert result.returncode == 0, f"crashed: {result.stderr}"

    pending = tmp_path / "system" / "pending_updates.md"
    assert pending.exists(), "pending_updates.md was not created"
    body = pending.read_text()
    assert "user_profile.md: add new role detail" in body
    assert "interaction_principles.md: tone adjustment" in body

    # Daily log should contain only the non-UPDATE lines
    daily = (tmp_path / "timeline" / "daily_log.md").read_text()
    assert "Normal day" in daily
    assert "→ UPDATE" not in daily


def test_empty_input_noop(tmp_path):
    result = _run("", tmp_path)
    assert result.returncode == 0
    assert not (tmp_path / "timeline" / "daily_log.md").exists()


def test_heartbeat_ok_noop(tmp_path):
    result = _run("HEARTBEAT_OK", tmp_path)
    assert result.returncode == 0
    assert not (tmp_path / "timeline" / "daily_log.md").exists()


def test_error_output_skipped(tmp_path):
    """Error-looking output should NOT pollute memory."""
    result = _run("Traceback (most recent call last):\n  File ...", tmp_path)
    assert result.returncode == 0
    assert not (tmp_path / "timeline" / "daily_log.md").exists()


def test_updates_only_creates_pending_header(tmp_path):
    stdin = "→ UPDATE: foo.md: bar\n"
    result = _run(stdin, tmp_path)
    assert result.returncode == 0
    pending = (tmp_path / "system" / "pending_updates.md").read_text()
    assert "Pending Memory Updates" in pending
    assert "foo.md: bar" in pending
