"""Tests for core.memory — flat loading (1M context era)."""

import time
from pathlib import Path

from core.memory import (
    HOT_BUDGET,
    MAX_MEMORY_CHARS,
    SYSTEM_BUDGET,
    TIMELINE_BUDGET,
    WARM_BUDGET,
    WARM_STALE_DAYS,
    all_facts,
    demote_stale_warm,
    get_fact,
    load_tiered_memory,
    set_fact,
)


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
    # A single hot file far exceeding the hot tier budget is truncated WITHIN
    # the hot tier (REQ-73) and the overall payload stays under MAX.
    hot = tmp_path / "hot"
    hot.mkdir()
    (hot / "big.md").write_text("x" * 250000)
    output = load_tiered_memory(tmp_path)
    assert "[hot memory truncated" in output
    assert len(output) < 250000
    assert len(output) <= MAX_MEMORY_CHARS


# ── REQ-73: per-tier budgets ─────────────────────────────────────────────


def _make_full_tree(tmp_path):
    for sub in ("hot", "warm", "system", "timeline"):
        (tmp_path / sub).mkdir()


def test_huge_warm_does_not_starve_timeline(tmp_path):
    """The core REQ-73 fix: an over-budget warm tier must NOT cause timeline
    content (freshest cross-day continuity) to be truncated away."""
    _make_full_tree(tmp_path)
    # warm far larger than the entire budget — would, under the old end-of-load
    # truncation, eat everything and drop timeline entirely.
    (tmp_path / "warm" / "huge.md").write_text("W" * (MAX_MEMORY_CHARS * 2))
    (tmp_path / "timeline" / "longterm_digest.md").write_text(
        "DIGEST_CONTINUITY_MARKER cross-day digest text"
    )
    (tmp_path / "timeline" / "hourly_log.md").write_text("HOURLY_MARKER")

    output = load_tiered_memory(tmp_path)

    # Timeline survived despite warm being massively over budget.
    assert "DIGEST_CONTINUITY_MARKER" in output
    assert "HOURLY_MARKER" in output
    # warm was truncated within its tier.
    assert "[warm memory truncated" in output
    # total respects the global cap.
    assert len(output) <= MAX_MEMORY_CHARS


def test_huge_warm_does_not_starve_structured_facts(tmp_path):
    """Hot reserve (incl. structured facts) is never dropped by a huge warm."""
    _make_full_tree(tmp_path)
    (tmp_path / "warm" / "huge.md").write_text("W" * (MAX_MEMORY_CHARS * 2))
    set_fact(tmp_path, "pascal_departure", "2026-06-24")
    output = load_tiered_memory(tmp_path)
    assert "pascal_departure: 2026-06-24" in output
    assert "Structured Facts" in output


def test_truncation_emits_warning(tmp_path, capsys):
    """Truncation must be observable: a stderr warning naming the tier and
    chars dropped (REQ-73 #2)."""
    _make_full_tree(tmp_path)
    (tmp_path / "warm" / "huge.md").write_text("W" * (MAX_MEMORY_CHARS * 2))
    load_tiered_memory(tmp_path)
    err = capsys.readouterr().err
    assert "warm tier truncated" in err
    assert "dropped" in err


def test_no_warning_when_within_budget(tmp_path, capsys):
    _make_full_tree(tmp_path)
    (tmp_path / "warm" / "small.md").write_text("small warm")
    (tmp_path / "timeline" / "hourly_log.md").write_text("small timeline")
    load_tiered_memory(tmp_path)
    err = capsys.readouterr().err
    assert "truncated" not in err


def test_tier_budgets_sum_to_max(tmp_path):
    # Sanity: reserves + warm remainder == MAX (warm absorbs the remainder).
    assert HOT_BUDGET + TIMELINE_BUDGET + SYSTEM_BUDGET + WARM_BUDGET == MAX_MEMORY_CHARS
    assert WARM_BUDGET > 0


def test_digest_prioritized_within_timeline(tmp_path):
    """When timeline is over its budget, the longterm_digest survives over the
    bulkier hourly log."""
    _make_full_tree(tmp_path)
    (tmp_path / "timeline" / "longterm_digest.md").write_text(
        "DIGEST_KEEP " + ("d" * 2000)
    )
    (tmp_path / "timeline" / "hourly_log.md").write_text(
        "HOURLY_BULK " + ("h" * (TIMELINE_BUDGET * 2))
    )
    output = load_tiered_memory(tmp_path)
    assert "DIGEST_KEEP" in output
    assert "[timeline memory truncated" in output


# ── REQ-73: stale warm demotion ──────────────────────────────────────────


def test_demote_stale_warm_moves_to_archive_and_loader_skips(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    stale = warm / "tianmushan_prep.md"
    fresh = warm / "health.md"
    stale.write_text("STALE_PREP_DOC for a past trip")
    fresh.write_text("FRESH_HEALTH_DATA")
    # Backdate the stale file well beyond the threshold.
    old = time.time() - (WARM_STALE_DAYS + 5) * 86400
    import os
    os.utime(stale, (old, old))

    demoted = demote_stale_warm(tmp_path)
    assert "tianmushan_prep.md" in demoted
    # File was MOVED, not deleted.
    assert not stale.exists()
    archived = warm / "archive" / "tianmushan_prep.md"
    assert archived.exists()
    assert "STALE_PREP_DOC" in archived.read_text()
    # Fresh file untouched.
    assert fresh.exists()

    # Loader skips warm/archive/ but still loads the fresh file.
    output = load_tiered_memory(tmp_path)
    assert "FRESH_HEALTH_DATA" in output
    assert "STALE_PREP_DOC" not in output


def test_demote_skips_fresh_and_index(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "_index.md").write_text("index")
    (warm / "recent.md").write_text("recent")
    # Backdate even the index past the threshold — it must still be protected.
    old = time.time() - (WARM_STALE_DAYS + 5) * 86400
    import os
    os.utime(warm / "_index.md", (old, old))
    demoted = demote_stale_warm(tmp_path)
    assert demoted == []
    assert (warm / "_index.md").exists()
    assert (warm / "recent.md").exists()


def test_demote_never_touches_other_tiers(tmp_path):
    """Demotion is warm-only; hot/system/timeline are never moved."""
    _make_full_tree(tmp_path)
    old = time.time() - (WARM_STALE_DAYS + 30) * 86400
    import os
    for sub, name in (("hot", "user_profile.md"),
                      ("system", "todos.md"),
                      ("timeline", "hourly_log.md")):
        p = tmp_path / sub / name
        p.write_text("content")
        os.utime(p, (old, old))
    demote_stale_warm(tmp_path)
    for sub, name in (("hot", "user_profile.md"),
                      ("system", "todos.md"),
                      ("timeline", "hourly_log.md")):
        assert (tmp_path / sub / name).exists()
    assert not (tmp_path / "hot" / "archive").exists()


# ── REQ-71: structured dated facts ───────────────────────────────────────


def test_set_get_fact_roundtrip(tmp_path):
    (tmp_path / "hot").mkdir()
    set_fact(tmp_path, "pascal_departure", "2026-06-24")
    set_fact(tmp_path, "partner_departure", "2026-06-14")
    assert get_fact(tmp_path, "pascal_departure") == "2026-06-24"
    assert get_fact(tmp_path, "partner_departure") == "2026-06-14"
    assert get_fact(tmp_path, "missing", "fallback") == "fallback"
    facts = all_facts(tmp_path)
    assert facts["pascal_departure"] == "2026-06-24"
    assert facts["partner_departure"] == "2026-06-14"


def test_set_fact_updates_in_place(tmp_path):
    (tmp_path / "hot").mkdir()
    set_fact(tmp_path, "pascal_departure", "2026-06-24")
    set_fact(tmp_path, "pascal_departure", "2026-06-25")  # correction
    assert get_fact(tmp_path, "pascal_departure") == "2026-06-25"
    # No duplicate DATA lines (commented example lines don't count).
    text = (tmp_path / "hot" / "structured_facts.md").read_text()
    data_lines = [
        ln for ln in text.splitlines()
        if ln.strip().startswith("pascal_departure:") and not ln.strip().startswith("#")
    ]
    assert len(data_lines) == 1


def test_set_fact_creates_file_with_template(tmp_path):
    # hot/ doesn't even exist yet — set_fact creates it.
    set_fact(tmp_path, "deadline", "2026-07-01")
    facts_file = tmp_path / "hot" / "structured_facts.md"
    assert facts_file.exists()
    assert get_fact(tmp_path, "deadline") == "2026-07-01"


def test_structured_facts_injected_first_in_hot(tmp_path):
    hot = tmp_path / "hot"
    hot.mkdir()
    (hot / "behavioral_rules.md").write_text("RULES_MARKER")
    (hot / "user_profile.md").write_text("PROFILE_MARKER")
    set_fact(tmp_path, "pascal_departure", "2026-06-24")
    output = load_tiered_memory(tmp_path)
    # Structured facts come before rules and profile.
    assert output.index("Structured Facts") < output.index("RULES_MARKER")
    assert output.index("pascal_departure: 2026-06-24") < output.index("PROFILE_MARKER")


def test_all_facts_empty_when_missing(tmp_path):
    assert all_facts(tmp_path) == {}
    assert get_fact(tmp_path, "anything") is None
