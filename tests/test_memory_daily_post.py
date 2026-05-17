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
    # Write entries: one recent, one old
    (tl / "daily_log.md").write_text(
        "## 2026-04-01\n- very old entry\n\n## 2026-05-16\n- recent entry\n"
    )
    result = _run("- Today's summary", tmp_path)
    assert result.returncode == 0

    daily = (tl / "daily_log.md").read_text()
    assert "recent entry" in daily
    assert "very old entry" not in daily

    archive = (tl / "daily_archive.md").read_text()
    assert "very old entry" in archive
