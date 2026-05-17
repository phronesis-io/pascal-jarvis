"""Tests for core.memory — flat loading (1M context era)."""

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
    (hot / "behavioral_rules.md").write_text("# Rules\nBe concise")
    output = load_tiered_memory(tmp_path)
    assert "Pascal" in output
    assert "Be concise" in output
    assert "Identity: user_profile" in output
    assert "Behavioral Rules" in output


def test_warm_files_now_loaded(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "health.md").write_text("health data loaded")
    output = load_tiered_memory(tmp_path)
    assert "health data loaded" in output
    assert "Knowledge: health" in output


def test_system_files_loaded(tmp_path):
    sys_dir = tmp_path / "system"
    sys_dir.mkdir()
    (sys_dir / "todos.md").write_text("- fix bug")
    (sys_dir / "open_threads.md").write_text("thread content")
    output = load_tiered_memory(tmp_path)
    assert "fix bug" in output
    assert "thread content" in output


def test_timeline_files_loaded(tmp_path):
    tl = tmp_path / "timeline"
    tl.mkdir()
    (tl / "hourly_log.md").write_text("hourly content")
    (tl / "daily_log.md").write_text("daily content")
    (tl / "longterm_digest.md").write_text("weekly content")
    output = load_tiered_memory(tmp_path)
    assert "Today's Hourly Log" in output
    assert "Recent Daily Summaries" in output
    assert "Weekly Digest" in output


def test_timeline_archives_not_loaded(tmp_path):
    tl = tmp_path / "timeline"
    tl.mkdir()
    (tl / "hourly_archive.md").write_text("archived stuff")
    (tl / "daily_archive.md").write_text("old daily stuff")
    (tl / "daily_log.md").write_text("recent stuff")
    output = load_tiered_memory(tmp_path)
    assert "archived stuff" not in output
    assert "old daily stuff" not in output
    assert "recent stuff" in output


def test_load_order_hot_warm_system_timeline(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    warm = tmp_path / "warm"
    warm.mkdir()
    sys_dir = tmp_path / "system"
    sys_dir.mkdir()
    tl = tmp_path / "timeline"
    tl.mkdir()
    (hot / "user_profile.md").write_text("HOTCONTENT")
    (warm / "health.md").write_text("WARMCONTENT")
    (sys_dir / "todos.md").write_text("SYSCONTENT")
    (tl / "hourly_log.md").write_text("TLCONTENT")

    output = load_tiered_memory(tmp_path)
    i_hot = output.index("HOTCONTENT")
    i_warm = output.index("WARMCONTENT")
    i_sys = output.index("SYSCONTENT")
    i_tl = output.index("TLCONTENT")
    assert i_hot < i_warm < i_sys < i_tl


def test_behavioral_rules_loaded_first(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    (hot / "behavioral_rules.md").write_text("RULES FIRST")
    (hot / "user_profile.md").write_text("PROFILE SECOND")
    output = load_tiered_memory(tmp_path)
    assert output.index("RULES FIRST") < output.index("PROFILE SECOND")


def test_empty_timeline_files_skipped(tmp_path):
    tl = tmp_path / "timeline"
    tl.mkdir()
    (tl / "hourly_log.md").write_text("")  # empty
    (tl / "daily_log.md").write_text("real content")
    output = load_tiered_memory(tmp_path)
    assert "Today's Hourly Log" not in output
    assert "Recent Daily Summaries" in output


def test_jsonl_system_files_not_loaded(tmp_path):
    sys_dir = tmp_path / "system"
    sys_dir.mkdir()
    (sys_dir / "activity_log.jsonl").write_text('{"event":"test"}')
    (sys_dir / "todos.md").write_text("real todo")
    output = load_tiered_memory(tmp_path)
    assert "real todo" in output
    # jsonl files should not appear (only .md loaded)
    assert "event" not in output


def test_truncation(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    # Write a file exceeding the 200K budget
    (hot / "big.md").write_text("x" * 250000)
    output = load_tiered_memory(tmp_path)
    assert "[memory truncated" in output
    assert len(output) < 250000
