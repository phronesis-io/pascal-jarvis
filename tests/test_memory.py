"""Tests for core.memory — tiered loading, skip rules."""

from pathlib import Path

from core.memory import load_tiered_memory


def test_empty_dir(tmp_path):
    assert load_tiered_memory(tmp_path) == ""


def test_missing_dir():
    assert load_tiered_memory("/nonexistent/path") == ""


def test_loads_hot_files(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    (hot / "user_profile.md").write_text("# Pascal\nAI founder")
    (hot / "interaction_principles.md").write_text("# Rules\nBe concise")
    output = load_tiered_memory(tmp_path)
    assert "Pascal" in output
    assert "Be concise" in output
    assert "Hot: user_profile" in output


def test_timeline_files_loaded(tmp_path):
    tl = tmp_path / "timeline"
    tl.mkdir()
    (tl / "hourly_log.md").write_text("hourly content")
    (tl / "daily_log.md").write_text("daily content")
    (tl / "longterm_digest.md").write_text("weekly content")
    (tl / "monthly_archive.md").write_text("monthly content")
    output = load_tiered_memory(tmp_path)
    assert "Today's Hourly Log" in output
    assert "Recent Daily Summaries" in output
    assert "Weekly Digest" in output
    assert "Monthly Archive" in output


def test_tiers_loaded_in_order(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    tl = tmp_path / "timeline"
    tl.mkdir()
    (hot / "user_profile.md").write_text("profile")
    (tl / "monthly_archive.md").write_text("monthly")
    (tl / "longterm_digest.md").write_text("weekly")
    (tl / "daily_log.md").write_text("daily")
    (tl / "hourly_log.md").write_text("hourly")

    output = load_tiered_memory(tmp_path)
    # Order: hot → monthly → weekly → daily → hourly
    i_profile = output.index("profile")
    i_monthly = output.index("monthly")
    i_weekly = output.index("weekly")
    i_daily = output.index("daily")
    i_hourly = output.index("hourly")
    assert i_profile < i_monthly < i_weekly < i_daily < i_hourly


def test_empty_timeline_files_skipped(tmp_path):
    tl = tmp_path / "timeline"
    tl.mkdir()
    (tl / "hourly_log.md").write_text("")  # empty
    (tl / "daily_log.md").write_text("real content")
    output = load_tiered_memory(tmp_path)
    assert "Today's Hourly Log" not in output  # empty file not injected
    assert "Recent Daily Summaries" in output


def test_system_todos_loaded(tmp_path):
    sys_dir = tmp_path / "system"
    sys_dir.mkdir()
    (sys_dir / "todos.md").write_text("- fix bug")
    (sys_dir / "open_threads.md").write_text("should not load")
    output = load_tiered_memory(tmp_path)
    assert "fix bug" in output
    assert "should not load" not in output


def test_warm_files_not_loaded(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "health.md").write_text("secret health data")
    output = load_tiered_memory(tmp_path)
    assert "secret health data" not in output


def test_index_loaded(tmp_path):
    (tmp_path / "_index.md").write_text("- health.md — fitness info")
    output = load_tiered_memory(tmp_path)
    assert "Memory Index" in output
    assert "health.md" in output


def test_truncation(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    # Write a file exceeding the 40K budget
    (hot / "big.md").write_text("x" * 50000)
    output = load_tiered_memory(tmp_path)
    assert "[memory truncated" in output
    assert len(output) < 50000
