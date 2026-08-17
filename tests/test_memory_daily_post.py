"""Tests for memory_daily_post.py — direct-write behavior.

Verifies that UPDATE directives are applied directly to target files (not queued),
daily log is written correctly, and hourly log is archived.
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


def test_same_day_retries_merge_under_one_heading(tmp_path):
    from datetime import date

    tl = tmp_path / "timeline"
    tl.mkdir()
    today = date.today().strftime("%Y-%m-%d")
    (tl / "daily_log.md").write_text(
        f"## {today}\n- first summary\n\n"
        f"## {today}\n- second summary\n"
    )

    assert _run("- second summary", tmp_path).returncode == 0
    assert _run("- third summary", tmp_path).returncode == 0

    daily = (tl / "daily_log.md").read_text()
    assert daily.count(f"## {today}") == 1
    assert daily.count("- second summary") == 1
    assert "- first summary" in daily
    assert "- third summary" in daily


def test_summary_with_update_directives_applies_directly(tmp_path):
    """UPDATE directives are applied directly to existing target files."""
    # Create target files so updates can be applied
    (tmp_path / "hot").mkdir()
    (tmp_path / "hot" / "user_profile.md").write_text("# Profile\n")
    stdin = (
        "- Normal day\n"
        "→ UPDATE: hot/user_profile.md: add new role detail\n"
        "→ UPDATE: warm/nonexistent.md: should be skipped\n"
    )
    result = _run(stdin, tmp_path)
    assert result.returncode == 0, f"crashed: {result.stderr}"

    # Target file should have the update appended
    profile = (tmp_path / "hot" / "user_profile.md").read_text()
    assert "add new role detail" in profile

    # pending_updates.md should NOT be created
    pending = tmp_path / "system" / "pending_updates.md"
    assert not pending.exists()

    # Daily log should contain only the non-UPDATE lines
    daily = (tmp_path / "timeline" / "daily_log.md").read_text()
    assert "Normal day" in daily
    assert "→ UPDATE" not in daily


def test_update_skips_nonexistent_files(tmp_path):
    """UPDATE for a file that doesn't exist is silently skipped."""
    stdin = "→ UPDATE: warm/ghost.md: this should not crash\n"
    result = _run(stdin, tmp_path)
    assert result.returncode == 0
    assert "does not exist" in result.stderr


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


def test_archive_old_entries(tmp_path):
    """Entries older than 14 days are moved to daily_archive."""
    tl = tmp_path / "timeline"
    tl.mkdir()
    (tmp_path / "system").mkdir()
    # Dates relative to *today* so the test doesn't rot once a hardcoded
    # "recent" date drifts past the 14-day archive threshold.
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")
    old = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")
    (tl / "daily_log.md").write_text(
        f"## {old}\n- very old entry\n\n## {recent}\n- recent entry\n"
    )
    result = _run("- Today's summary", tmp_path)
    assert result.returncode == 0

    daily = (tl / "daily_log.md").read_text()
    assert "recent entry" in daily
    assert "very old entry" not in daily

    archive = (tl / "daily_archive.md").read_text()
    assert "very old entry" in archive
