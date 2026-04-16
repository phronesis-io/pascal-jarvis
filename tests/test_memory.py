"""Tests for core.memory — tiered loading, skip rules."""

from pathlib import Path

from core.memory import _SKIP_TIER1, load_tiered_memory


def test_empty_dir(tmp_path):
    assert load_tiered_memory(tmp_path) == ""


def test_missing_dir():
    assert load_tiered_memory("/nonexistent/path") == ""


def test_loads_permanent_files(tmp_path):
    (tmp_path / "user_profile.md").write_text("# Pascal\nAI founder")
    (tmp_path / "interaction_principles.md").write_text("# Rules\nBe concise")
    output = load_tiered_memory(tmp_path)
    assert "Pascal" in output
    assert "Be concise" in output
    assert "## user_profile" in output


def test_skips_working_logs_from_tier1(tmp_path):
    """Working log files should be loaded separately as tiers 2-4, not tier 1."""
    for name in _SKIP_TIER1:
        (tmp_path / name).write_text(f"content of {name}")
    # Permanent file
    (tmp_path / "user_profile.md").write_text("profile")

    output = load_tiered_memory(tmp_path)
    # Permanent file loaded as tier 1
    assert "## user_profile" in output
    # MEMORY.md is fully skipped (not loaded anywhere)
    assert "## MEMORY" not in output
    # hourly_log.md gets a "Today's Hourly Log" header, not "## hourly_log"
    assert "## hourly_log" not in output


def test_tiers_loaded_in_order(tmp_path):
    (tmp_path / "user_profile.md").write_text("profile")
    (tmp_path / "hourly_log.md").write_text("hourly")
    (tmp_path / "daily_log.md").write_text("daily")
    (tmp_path / "longterm_digest.md").write_text("weekly")
    (tmp_path / "monthly_archive.md").write_text("monthly")

    output = load_tiered_memory(tmp_path)
    # Order: tier 1 → monthly → weekly → daily → hourly
    i_profile = output.index("profile")
    i_monthly = output.index("monthly")
    i_weekly = output.index("weekly")
    i_daily = output.index("daily")
    i_hourly = output.index("hourly")
    assert i_profile < i_monthly < i_weekly < i_daily < i_hourly


def test_empty_log_files_skipped(tmp_path):
    (tmp_path / "hourly_log.md").write_text("")  # empty
    (tmp_path / "daily_log.md").write_text("real content")
    output = load_tiered_memory(tmp_path)
    assert "Today's Hourly Log" not in output  # empty file not injected
    assert "Recent Daily Summaries" in output
