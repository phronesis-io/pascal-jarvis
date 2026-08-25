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


def _fill_warm_over_budget(tmp_path):
    """Push the GLOBAL payload over MAX via many under-cap warm files.

    The old single huge.md trick stopped working when WARM_FILE_CAP landed
    (记忆瘦身 PRD R4) — one file now caps at 12k, so the squeeze these tests
    exercise never engaged. Volume through file COUNT instead."""
    warm = tmp_path / "warm"
    warm.mkdir(exist_ok=True)
    for i in range(40):
        (warm / f"bulk_{i:02d}.md").write_text(f"W_{i:02d} " + "W" * 10800)


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
    _fill_warm_over_budget(tmp_path)
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
    _fill_warm_over_budget(tmp_path)
    set_fact(tmp_path, "pascal_departure", "2026-06-24")
    output = load_tiered_memory(tmp_path)
    assert "pascal_departure: 2026-06-24" in output
    assert "Structured Facts" in output


def test_truncation_emits_warning(tmp_path, capsys):
    """Truncation must be observable: a stderr warning naming the tier and
    chars dropped (REQ-73 #2)."""
    _make_full_tree(tmp_path)
    _fill_warm_over_budget(tmp_path)
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


def test_system_caps_cannot_overflow_their_budget():
    """A budget whose members are unbounded is not a budget.

    Three times (7/14, 7/21, 7/29) the system tier overflowed and the tail —
    always the inbox buffers, i.e. ALL of mail-triage's output — was cut on
    every single cycle. Twice the fix raised the budget and capped the files
    being dropped; both times an uncapped file (open_threads, todos,
    engineering_roadmap) grew back into the gap. This asserts the arithmetic
    that makes the failure impossible rather than merely unlikely.
    """
    from core.memory import (_SYSTEM_FILE_CAPS, _SYSTEM_FILE_DEFAULT_CAP,
                             _SYSTEM_UNDECLARED_ALLOWANCE)

    declared = sum(_SYSTEM_FILE_CAPS.values())
    assert declared + _SYSTEM_UNDECLARED_ALLOWANCE <= SYSTEM_BUDGET, (
        f"system caps {declared:,} + undeclared allowance "
        f"{_SYSTEM_UNDECLARED_ALLOWANCE:,} exceed SYSTEM_BUDGET "
        f"{SYSTEM_BUDGET:,} — the tail would be dropped every cycle"
    )
    # Room for files nobody has declared yet, at the default cap.
    assert _SYSTEM_UNDECLARED_ALLOWANCE >= _SYSTEM_FILE_DEFAULT_CAP


def test_every_system_file_is_capped(tmp_path):
    """An undeclared system file must still be bounded, not unbounded."""
    from core.memory import _SYSTEM_FILE_DEFAULT_CAP

    sys_dir = tmp_path / "system"
    sys_dir.mkdir(parents=True)
    (sys_dir / "a_brand_new_buffer.md").write_text(
        "HEAD\n" + ("z" * (_SYSTEM_FILE_DEFAULT_CAP * 4)), encoding="utf-8")
    out = load_tiered_memory(tmp_path)
    body = out.split("## System: a_brand_new_buffer", 1)[1]
    assert len(body) <= _SYSTEM_FILE_DEFAULT_CAP + 200


def test_mail_inbox_survives_a_fat_open_threads(tmp_path):
    """The exact production shape on 7/29: load-bearing files big enough to
    eat the whole budget, with the mail buffer last in priority order."""
    sys_dir = tmp_path / "system"
    sys_dir.mkdir(parents=True)
    for name in ("open_threads.md", "todos.md", "engineering_roadmap.md"):
        (sys_dir / name).write_text("x" * 40000, encoding="utf-8")
    (sys_dir / "inbox_private_mail.md").write_text(
        "OLD\n" + ("m" * 30000) + "\nNEWEST_MAIL_MARKER", encoding="utf-8")
    out = load_tiered_memory(tmp_path)
    assert "## System: inbox_private_mail" in out
    # inbox buffers are tail-keep: the newest mail is what must survive.
    assert "NEWEST_MAIL_MARKER" in out


def test_digest_prioritized_within_timeline(tmp_path):
    """When the GLOBAL payload exceeds MAX (so tier budgeting kicks in), the
    longterm_digest survives over the bulkier hourly log within the timeline
    reserve. Tier truncation only fires when global > MAX (red-team fix:
    headroom is borrowed, never thrown away)."""
    _make_full_tree(tmp_path)
    # Push the GLOBAL payload over MAX so per-tier budgeting engages.
    _fill_warm_over_budget(tmp_path)
    (tmp_path / "timeline" / "longterm_digest.md").write_text(
        "DIGEST_KEEP " + ("d" * 2000)
    )
    (tmp_path / "timeline" / "hourly_log.md").write_text(
        "HOURLY_BULK " + ("h" * (TIMELINE_BUDGET * 2))
    )
    output = load_tiered_memory(tmp_path)
    assert "DIGEST_KEEP" in output                 # priority 0 survives
    assert "[timeline memory truncated" in output  # tier budget engaged


def test_under_budget_loads_everything_no_truncation(tmp_path):
    """Red-team fix: with global headroom, NO tier is truncated — load-bearing
    system files (open_threads/todos) must not be dropped just because a tier
    is over its reserve while the total fits under MAX."""
    _make_full_tree(tmp_path)
    sysd = tmp_path / "system"
    sysd.mkdir(exist_ok=True)
    # A bulky inbox over SYSTEM_BUDGET, plus small load-bearing files.
    (sysd / "inbox_ops.md").write_text("OPS " + ("o" * 50000))
    (sysd / "open_threads.md").write_text("OPEN_THREADS_KEEP active follow-up")
    (sysd / "todos.md").write_text("TODOS_KEEP - [ ] do the thing")
    output = load_tiered_memory(tmp_path)
    # Total is well under MAX (200k), so nothing is dropped.
    assert "OPEN_THREADS_KEEP" in output
    assert "TODOS_KEEP" in output
    assert "[system memory truncated" not in output


def test_todos_hard_cut_keeps_tail(tmp_path):
    """Append-only todos.md: when the system tier is cut mid-file, the NEWEST
    entries (tail) must survive — head-keep had the model reading April todos
    while the same-day entries were dropped (2026-07-07 memory audit). The
    omission note must sit ABOVE the kept tail and say the OLDEST entries
    were cut — the old bottom marker read as "newest entries truncated", the
    exact confusion tail-keep was built to remove (2026-07-08 red-team fix)."""
    _make_full_tree(tmp_path)
    # Force the over-budget branch so tier budgeting engages.
    _fill_warm_over_budget(tmp_path)
    (tmp_path / "system" / "todos.md").write_text(
        "APRIL_HEAD_MARKER oldest entry\n"
        + ("x" * (SYSTEM_BUDGET * 2))
        + "\nJULY_TAIL_MARKER newest entry"
    )
    output = load_tiered_memory(tmp_path)
    assert "JULY_TAIL_MARKER" in output
    assert "APRIL_HEAD_MARKER" not in output
    assert "## System: todos" in output          # header survives the cut
    # Head-omission note: right after the header, above the kept tail.
    assert "oldest ~" in output
    assert "full file on disk: todos.md" in output
    assert output.index("## System: todos") < output.index("oldest ~")
    assert output.index("oldest ~") < output.index("JULY_TAIL_MARKER")
    # No trailing "memory truncated" marker after the newest entries — that
    # was the inverted-semantics bug.
    assert "[system memory truncated" not in output


def test_todos_tail_snaps_to_entry_boundary(tmp_path):
    """The kept tail opens on a '<!-- auto-update' entry boundary when one is
    in range, never mid-entry (2026-07-08 red-team fix)."""
    _make_full_tree(tmp_path)
    _fill_warm_over_budget(tmp_path)
    (tmp_path / "system" / "todos.md").write_text(
        "APRIL_HEAD_MARKER oldest entry\n"
        + ("x" * (SYSTEM_BUDGET * 2))
        + "\nhalf-an-entry fragment\n<!-- auto-update 2026-07-08 -->\n"
        + "- JULY_TAIL_MARKER newest entry"
    )
    output = load_tiered_memory(tmp_path)
    assert "JULY_TAIL_MARKER" in output
    assert "half-an-entry fragment" not in output
    # The tail starts exactly at the boundary comment.
    note_end = output.index("full file on disk: todos.md]\n") \
        + len("full file on disk: todos.md]\n")
    assert output[note_end:].startswith("<!-- auto-update")


def test_curated_system_files_still_keep_head(tmp_path):
    """Tail-keep is per-file (todos.md only): curated files like open_threads
    are not append-ordered, so they keep their head when cut."""
    _make_full_tree(tmp_path)
    _fill_warm_over_budget(tmp_path)
    (tmp_path / "system" / "open_threads.md").write_text(
        "HEAD_THREAD_MARKER\n" + ("y" * (SYSTEM_BUDGET * 2)) + "\nTAIL_NOISE_MARKER"
    )
    output = load_tiered_memory(tmp_path)
    assert "HEAD_THREAD_MARKER" in output
    assert "TAIL_NOISE_MARKER" not in output


def test_inbox_buffers_capped_at_load(tmp_path):
    """Per-file caps: the perception inbox buffers (2×~40KB > the whole 40k
    system reserve) can never again evict cross_session_digest & co — they
    are tail-capped at load time, newest signals kept, file on disk intact
    (2026-07-07 memory audit)."""
    _make_full_tree(tmp_path)
    sysd = tmp_path / "system"
    (sysd / "inbox_ops.md").write_text(
        "OLD_SIGNAL_MARKER\n" + ("o" * 20000) + "\nNEW_SIGNAL_MARKER")
    (sysd / "cross_session_digest.md").write_text("DIGEST_MARKER")
    output = load_tiered_memory(tmp_path)
    assert "NEW_SIGNAL_MARKER" in output
    assert "OLD_SIGNAL_MARKER" not in output
    assert "[capped" in output
    assert "DIGEST_MARKER" in output
    # Cap is load-time only — the file on disk is untouched.
    assert "OLD_SIGNAL_MARKER" in (sysd / "inbox_ops.md").read_text()


def test_truncation_leveled_warn_rate_limited_per_tier(tmp_path, capsys):
    """The truncation event must reach the structured leveled logger (selfmon
    consumes leveled JSON, not bare stderr prose) — rate-limited to once per
    tier per hour, not 400+ identical warns/day (2026-07-07 memory audit),
    but NOT once per process lifetime: a long-lived heartbeat must re-surface
    a persistent or new truncation episode (2026-07-08 red-team fix). The
    bare stderr line stays for backward compat."""
    import core.memory as memory_mod
    memory_mod._TRUNCATION_WARNED_AT.clear()
    _make_full_tree(tmp_path)
    _fill_warm_over_budget(tmp_path)
    load_tiered_memory(tmp_path)
    load_tiered_memory(tmp_path)
    err = capsys.readouterr().err
    assert err.count("warm tier truncated") >= 2   # bare line every time
    assert err.count('"msg": "tier_truncated"') == 1  # leveled warn deduped
    assert '"level": "warn"' in err
    # >1h since the last warn for this tier → warns again (long-lived
    # heartbeat process, day-10 episode).
    memory_mod._TRUNCATION_WARNED_AT["warm"] -= (
        memory_mod._TRUNCATION_WARN_INTERVAL_S + 1)
    load_tiered_memory(tmp_path)
    err = capsys.readouterr().err
    assert err.count('"msg": "tier_truncated"') == 1


def test_truncation_warn_names_partially_cut_section(tmp_path, capsys):
    """When the cut lands INSIDE the last/only big section, dropped_sections
    must name it ('<header> (partial)') — it was [] exactly in the single-
    big-file case the 2026-07 audit was about (2026-07-08 red-team fix)."""
    import core.memory as memory_mod
    memory_mod._TRUNCATION_WARNED_AT.clear()
    _make_full_tree(tmp_path)
    _fill_warm_over_budget(tmp_path)
    load_tiered_memory(tmp_path)
    err = capsys.readouterr().err
    assert "(partial)" in err


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


def test_set_fact_rejects_newline_injection(tmp_path):
    """Red-team P2: a newline in value injected a phantom fact."""
    from core.memory import set_fact, all_facts
    (tmp_path / "hot").mkdir()
    set_fact(tmp_path, "note", "line1\nfake_key: injected")
    facts = all_facts(tmp_path)
    assert "fake_key" not in facts
    assert facts["note"] == "line1 fake_key: injected"  # newline collapsed, single fact


def test_set_fact_atomic_and_dedups(tmp_path):
    from core.memory import set_fact, get_fact
    (tmp_path / "hot").mkdir()
    set_fact(tmp_path, "k", "A")
    set_fact(tmp_path, "k", "B")
    assert get_fact(tmp_path, "k") == "B"
    # only one line for k
    text = (tmp_path / "hot" / "structured_facts.md").read_text()
    assert text.count("k: ") == 1


# ── REQ-91: budget/cap arithmetic must stay self-consistent ────────────────


def test_system_budget_covers_named_caps():
    """Superseded model, kept as a named guard against regressing to it.

    The 2026-07-14 version summed TODOS_MAX_CHARS + the two named caps + a 20k
    slack term standing in for the UNCAPPED working set. That slack term was
    the bug: open_threads/todos/roadmap grew straight through it and the tier
    overflowed again on 7/21 and 7/29. Now every file is capped, so the
    arithmetic is exact and lives in test_system_caps_cannot_overflow_their
    _budget. What remains worth asserting here is that tidy's ON-DISK
    retention for todos never exceeds what the loader will actually inject —
    the "dark matter" REQ-92 named: bytes kept on disk that no prompt ever
    sees.
    """
    import core.memory as m
    from tasks.memory_tidy_post import TODOS_MAX_CHARS
    assert TODOS_MAX_CHARS <= m._SYSTEM_FILE_CAPS["todos.md"], (
        f"tidy keeps {TODOS_MAX_CHARS:,} chars of todos on disk but the loader "
        f"injects at most {m._SYSTEM_FILE_CAPS['todos.md']:,} — the difference "
        f"is never read by anything"
    )


# ── 2026-07-21 记忆瘦身 PRD: protected band / warm cap / demote exemption ──


def _make_over_budget_warm(tmp_path):
    """Warm dir whose assembled size forces the global squeeze."""
    import os
    warm = tmp_path / "warm"
    warm.mkdir()
    # Timeless guidance, mtime ancient (the old first casualty).
    guidance = warm / "feedback_steelman.md"
    guidance.write_text("GUIDANCE_MARKER steelman every critique")
    user_pref = warm / "user_identity.md"
    user_pref.write_text("USER_PREF_MARKER pronouns and names")
    ancient = time.time() - 300 * 86400
    os.utime(guidance, (ancient, ancient))
    os.utime(user_pref, (ancient, ancient))
    # Fat fresh docs that alone exceed MAX_MEMORY_CHARS.
    for i in range(30):
        f = warm / f"project_fat_{i:02d}.md"
        f.write_text(f"FAT_{i:02d} " + "x" * 11000)
    return warm


def test_warm_protected_band_survives_squeeze(tmp_path):
    out = load_tiered_memory(tmp_path)
    # sanity: empty dir baseline
    warm = _make_over_budget_warm(tmp_path)
    out = load_tiered_memory(tmp_path)
    assert len(out) <= MAX_MEMORY_CHARS + 100
    # Old-but-timeless guidance is present; by pure mtime it would be first out.
    assert "GUIDANCE_MARKER" in out
    assert "USER_PREF_MARKER" in out
    # The squeeze dropped fat docs instead (at least one fell off the end).
    assert "[warm memory truncated" in out or out.count("FAT_") < 30
    assert warm.exists()


def test_custom_memory_budget_is_a_hard_cap_and_preserves_tiers(tmp_path):
    hot = tmp_path / "hot"
    system = tmp_path / "system"
    timeline = tmp_path / "timeline"
    hot.mkdir()
    system.mkdir()
    timeline.mkdir()
    (hot / "structured_facts.md").write_text("HOT_BUDGET_MARKER")
    (system / "open_threads.md").write_text("SYSTEM_BUDGET_MARKER")
    (timeline / "daily_log.md").write_text("TIMELINE_BUDGET_MARKER")
    _make_over_budget_warm(tmp_path)

    output = load_tiered_memory(tmp_path, max_chars=10_000)

    assert len(output) <= 10_000
    assert "HOT_BUDGET_MARKER" in output
    assert "SYSTEM_BUDGET_MARKER" in output
    assert "TIMELINE_BUDGET_MARKER" in output


def test_backup_budget_preserves_complete_hot_identity_before_warm_notes(
    tmp_path, monkeypatch,
):
    import core.memory as memory_mod
    events = []
    monkeypatch.setattr(memory_mod, "log", lambda *args, **kwargs: events.append(kwargs))
    memory_mod._TRUNCATION_WARNED_AT.clear()
    hot = tmp_path / "hot"
    system = tmp_path / "system"
    timeline = tmp_path / "timeline"
    warm = tmp_path / "warm"
    for path in (hot, system, timeline, warm):
        path.mkdir()
    (hot / "structured_facts.md").write_text("facts\n" + "f" * 1500)
    (hot / "behavioral_rules.md").write_text(
        "RULES_START\n" + "r" * 10_000 + "\nRULES_END"
    )
    for name in (
        "active_intents.md", "calendar_today.md", "feedback_rules.md",
        "group_context.md", "user_healing_frame.md",
    ):
        (hot / name).write_text(name + "\n" + "h" * 1800)
    (hot / "user_profile.md").write_text(
        "user_profile\n" + "p" * 1800 + "\nHOT_LAST_USER_PROFILE"
    )
    (system / "open_threads.md").write_text(
        "SYSTEM_PRIORITY\n" + "s" * 18_000
    )
    (timeline / "daily_log.md").write_text(
        "TIMELINE_PRIORITY\n" + "t" * 5000
    )
    (warm / "project_research.md").write_text(
        "WARM_RESEARCH\n" + "w" * 40_000
    )

    output = load_tiered_memory(tmp_path, max_chars=40_000)

    assert len(output) <= 40_000
    assert "RULES_START" in output and "RULES_END" in output
    assert "HOT_LAST_USER_PROFILE" in output
    assert "SYSTEM_PRIORITY" in output
    assert "TIMELINE_PRIORITY" in output
    assert events
    assert all(event["expected"] is True for event in events)


def test_reduced_budget_prioritizes_memory_relevant_to_current_message(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "feedback_rules.md").write_text(
        "GENERAL_GUIDANCE\n" + "g" * 12_000
    )
    (warm / "insurance_research.md").write_text(
        "董事责任保险 董责险 配偶 Agent 转发结论\n" + "i" * 3000
    )

    unfocused = load_tiered_memory(tmp_path, max_chars=5000)
    focused = load_tiered_memory(
        tmp_path,
        max_chars=5000,
        focus_text="继续处理董责险并转发给我老婆的 agent",
    )

    assert "董事责任保险" not in unfocused
    assert "董事责任保险" in focused
    assert len(focused) <= 5000


def test_warm_per_file_cap_head_keep(tmp_path):
    from core.memory import WARM_FILE_CAP
    warm = tmp_path / "warm"
    warm.mkdir()
    big = warm / "project_roadmap.md"
    big.write_text("HEAD_SUMMARY first\n" + ("line of detail\n" * 3000)
                   + "TAIL_APPENDIX last")
    out = load_tiered_memory(tmp_path)
    # Head kept (knowledge docs front-load their summary), tail capped away.
    assert "HEAD_SUMMARY" in out
    assert "TAIL_APPENDIX" not in out
    assert "full file on disk: project_roadmap.md" in out
    section = out.split("## Knowledge: project_roadmap", 1)[1]
    assert len(section) <= WARM_FILE_CAP + 200


def test_demote_skips_protected_guidance(tmp_path):
    import os
    warm = tmp_path / "warm"
    warm.mkdir()
    old = time.time() - (WARM_STALE_DAYS + 10) * 86400
    guidance = warm / "feedback_code_review.md"
    guidance.write_text("timeless rule")
    prep = warm / "trip_prep_2026.md"
    prep.write_text("stale prep doc")
    for f in (guidance, prep):
        os.utime(f, (old, old))
    demoted = demote_stale_warm(tmp_path)
    assert "trip_prep_2026.md" in demoted
    assert "feedback_code_review.md" not in demoted
    assert guidance.exists()
    assert not prep.exists()


def test_demote_exempts_frontmatter_types_and_named_targets(tmp_path):
    """红队修正：前缀之外，frontmatter 自我声明 type: user/feedback/
    question/project 的文件与 consolidate 硬编码目标 projects.md 一律豁免。"""
    import os
    warm = tmp_path / "warm"
    warm.mkdir()
    old = time.time() - (WARM_STALE_DAYS + 10) * 86400
    typed_user = warm / "healing_coords.md"
    typed_user.write_text("---\ntype: user\n---\ncoordinates")
    typed_project = warm / "morning_routine.md"
    typed_project.write_text("---\ntype: project\nstatus: in_progress\n---\nroutine")
    named = warm / "projects.md"
    named.write_text("个人项目总表")
    plain_prep = warm / "conference_prep.md"
    plain_prep.write_text("one-off prep notes")
    for f in (typed_user, typed_project, named, plain_prep):
        os.utime(f, (old, old))

    demoted = demote_stale_warm(tmp_path)

    assert demoted == ["conference_prep.md"]
    assert typed_user.exists() and typed_project.exists() and named.exists()


def test_warm_head_keep_floor_on_unbroken_blob(tmp_path):
    """短首行+单段 12k 长文不能坍缩成 4 个字符（snap 需要下限）。"""
    from core.memory import WARM_FILE_CAP
    warm = tmp_path / "warm"
    warm.mkdir()
    blob = warm / "pasted_blob.md"
    blob.write_text("HEAD\n" + "字" * (WARM_FILE_CAP + 5000))
    out = load_tiered_memory(tmp_path)
    section = out.split("## Knowledge: pasted_blob", 1)[1]
    # 保留量接近 cap，而不是只剩首行。
    assert len(section) > WARM_FILE_CAP - 500
    assert "full file on disk: pasted_blob.md" in out


def test_warm_index_mode_keeps_rules_and_maps_the_rest(tmp_path):
    """index 模式：标准行为规则照旧全文进上下文，参考笔记降级成一行索引。

    规则不能懒加载——模型不知道自己需要某条规则时不会去 fetch 它。
    """
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "feedback_never_lazy.md").write_text(
        "---\ndescription: 标准规则\n---\n\n必须全文注入的行为准则正文。\n",
        encoding="utf-8")
    (warm / "project_big_note.md").write_text(
        "---\ndescription: 一份很长的参考笔记\n---\n\n" + "内容" * 3000,
        encoding="utf-8")

    full = load_tiered_memory(tmp_path)
    idx = load_tiered_memory(tmp_path, warm_mode="index")

    # 规则两种模式下都在
    assert "必须全文注入的行为准则正文。" in full
    assert "必须全文注入的行为准则正文。" in idx

    # 参考笔记只在 full 里展开；index 里只留一行指路
    assert "内容内容" in full
    assert "内容内容" not in idx
    assert "project_big_note.md" in idx
    assert "一份很长的参考笔记" in idx
    assert str(warm) in idx
    assert len(idx) < len(full)


def test_warm_index_puts_stable_identity_and_guidance_before_volatile_state(
        tmp_path):
    """Prompt-cache prefix stays reusable while calendar/intents keep moving."""
    hot = tmp_path / "hot"
    warm = tmp_path / "warm"
    hot.mkdir()
    warm.mkdir()
    (hot / "behavioral_rules.md").write_text("STABLE_RULES")
    (hot / "user_profile.md").write_text("STABLE_PROFILE")
    (hot / "active_intents.md").write_text("VOLATILE_INTENTS")
    (hot / "calendar_today.md").write_text("VOLATILE_CALENDAR")
    (warm / "feedback_never_guess.md").write_text("STABLE_GUIDANCE")
    (warm / "project_note.md").write_text(
        "---\ndescription: reference summary\n---\nREFERENCE_BODY")

    output = load_tiered_memory(tmp_path, warm_mode="index")

    stable_end = output.index("STABLE_GUIDANCE")
    assert output.index("STABLE_RULES") < stable_end
    assert output.index("STABLE_PROFILE") < stable_end
    assert stable_end < output.index("VOLATILE_INTENTS")
    assert stable_end < output.index("VOLATILE_CALENDAR")
    assert output.index("VOLATILE_CALENDAR") < output.index("Knowledge Index")
    assert "REFERENCE_BODY" not in output


def test_warm_mode_rejects_unknown_value(tmp_path):
    import pytest
    (tmp_path / "warm").mkdir()
    with pytest.raises(ValueError):
        load_tiered_memory(tmp_path, warm_mode="lazy")


def test_warm_index_falls_back_to_body_line_without_frontmatter(tmp_path):
    """没有 description 就用正文首行，绝不替它编一句摘要。"""
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "notes_plain.md").write_text(
        "# 标题\n\n这是正文第一行。\n" + "尾" * 100, encoding="utf-8")
    idx = load_tiered_memory(tmp_path, warm_mode="index")
    assert "这是正文第一行。" in idx
    assert "# 标题" not in idx
