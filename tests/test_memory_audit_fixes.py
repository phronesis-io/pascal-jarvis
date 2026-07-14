"""Tests for the 2026-07-08 memory-audit fixes (F8 / F9 / F22).

F8  — empty UPDATE directives must be skipped (no empty-bullet tombstones over
      real content) and the "applied N" stderr line must count successes, not
      directives. Same guard in memory_daily_post, whose \\s* regex has a
      different failure shape (slurps the next line; empty only at end-of-output).
F9  — warm/ directives resolved under the heartbeat MEMORY_DIR must land in
      the canonical auto-memory warm/ (CLAUDE.md source-of-truth rule), so
      memory-tidy's one-way auto→heartbeat sync stays the replica's only writer.
F22 — session_compact must bind the two memory dirs by EXACT project slug:
      endswith() suffix scanning bound AUTO_MEMORY to the heartbeat dir (its
      slug also ends with "-jarvis") and starved jarvis-root of compaction.

All memory trees here are tmp_path fakes — never the real memory dirs.
"""

import importlib.util
import io
import os
import time
from pathlib import Path

import pytest

import tasks.memory_consolidate_post as mcp
import tasks.memory_daily_post as mdp
import tasks.memory_tidy_post as mtp

TS = "2026-07-08"


def _make_tree(base: Path, name: str) -> Path:
    mem = base / name
    for tier in ("hot", "warm", "system", "timeline"):
        (mem / tier).mkdir(parents=True)
    return mem


@pytest.fixture
def plain_memory(tmp_path, monkeypatch):
    """A memory tree that is NOT the heartbeat replica (no warm/ redirect)."""
    mem = _make_tree(tmp_path, "plain")
    # Point the canon/replica pair elsewhere so equality can never match mem.
    monkeypatch.setattr(mcp, "AUTO_MEMORY", _make_tree(tmp_path, "auto_unused"))
    monkeypatch.setattr(mcp, "HEARTBEAT_MEMORY", tmp_path / "hb_unused")
    return mem


@pytest.fixture
def canon_and_replica(tmp_path, monkeypatch):
    """Fake canon (auto) + replica (heartbeat) pair, patched into the module."""
    auto = _make_tree(tmp_path, "auto")
    hb = _make_tree(tmp_path, "heartbeat")
    monkeypatch.setattr(mcp, "AUTO_MEMORY", auto)
    monkeypatch.setattr(mcp, "HEARTBEAT_MEMORY", hb)
    return auto, hb


# ── F8: empty-UPDATE guard + honest count (memory_consolidate_post) ──────


def test_update_empty_after_marker_strip_skipped(plain_memory, capsys):
    """The observed failure: model echoes the auto-update marker as the whole
    same-line content; after the strip it's empty and used to tombstone."""
    target = plain_memory / "warm" / "team.md"
    original = "# Team\n- 真实的旧内容\n"
    target.write_text(original, encoding="utf-8")

    ok = mcp._apply_update(plain_memory, "warm/team.md",
                           "<!-- auto-update 2026-07-08 -->", TS)

    assert ok is False
    assert target.read_text(encoding="utf-8") == original  # no empty bullet
    assert "empty content" in capsys.readouterr().err


def test_update_whitespace_content_skipped(plain_memory, capsys):
    target = plain_memory / "warm" / "team.md"
    target.write_text("# Team\n", encoding="utf-8")
    assert mcp._apply_update(plain_memory, "warm/team.md", "   ", TS) is False
    assert target.read_text(encoding="utf-8") == "# Team\n"
    assert "empty content" in capsys.readouterr().err


def test_update_real_content_still_applies(plain_memory):
    target = plain_memory / "warm" / "team.md"
    target.write_text("# Team\n", encoding="utf-8")
    assert mcp._apply_update(plain_memory, "warm/team.md", "新事实一条", TS) is True
    text = target.read_text(encoding="utf-8")
    assert f"<!-- auto-update {TS} -->\n- 新事实一条" in text


def test_main_counts_only_successful_updates(plain_memory, tmp_path, monkeypatch, capsys):
    """'applied N' must be honest: 1 good + 1 empty directive → applied 1/2."""
    target = plain_memory / "warm" / "team.md"
    target.write_text("# Team\n", encoding="utf-8")

    monkeypatch.setattr(mcp, "MEMORY_DIR", plain_memory)
    monkeypatch.setattr(mcp, "JARVIS_DIR", tmp_path / "jarvis")  # diary → tmp
    stdin = (
        "今晚整理日记，内容照旧。\n"
        "→ UPDATE: warm/team.md: <!-- auto-update 2026-07-08 -->\n"
        "本该在指令行上的正文（旧行为下会变成空弹头）。\n"
        "→ UPDATE: warm/team.md: 小王加入团队\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))

    assert mcp.main() == 0

    err = capsys.readouterr().err
    assert "applied 1/2 update(s) directly" in err
    text = target.read_text(encoding="utf-8")
    assert "小王加入团队" in text
    assert not any(line.strip() == "-" for line in text.splitlines())


# ── F8: same guard in memory_daily_post ──────────────────────────────────


def test_daily_update_empty_content_skipped(tmp_path, capsys):
    mem = _make_tree(tmp_path, "daily")
    target = mem / "hot" / "user_profile.md"
    target.write_text("# Profile\n", encoding="utf-8")

    mdp._apply_update(mem, "hot/user_profile.md", "   ", TS)

    assert target.read_text(encoding="utf-8") == "# Profile\n"
    assert "empty content" in capsys.readouterr().err


def test_daily_update_real_content_still_applies(tmp_path):
    mem = _make_tree(tmp_path, "daily")
    target = mem / "hot" / "user_profile.md"
    target.write_text("# Profile\n", encoding="utf-8")

    mdp._apply_update(mem, "hot/user_profile.md", "新角色", TS)

    assert f"<!-- auto-update {TS} -->\n- 新角色" in target.read_text(encoding="utf-8")


# ── F9: warm/ redirect to canonical auto-memory ──────────────────────────


def test_warm_update_under_heartbeat_lands_in_canon(canon_and_replica, capsys):
    auto, hb = canon_and_replica
    (auto / "warm" / "team.md").write_text("# Team canon\n", encoding="utf-8")
    replica_before = "# Team replica（更新过，等 reconcile）\n"
    (hb / "warm" / "team.md").write_text(replica_before, encoding="utf-8")

    ok = mcp._apply_update(hb, "warm/team.md", "新事实", TS)

    assert ok is True
    assert "新事实" in (auto / "warm" / "team.md").read_text(encoding="utf-8")
    # tidy's sync stays the replica's ONLY writer
    assert (hb / "warm" / "team.md").read_text(encoding="utf-8") == replica_before
    assert "rerouted to canonical auto-memory" in capsys.readouterr().err


def test_warm_replace_under_heartbeat_lands_in_canon(canon_and_replica):
    auto, hb = canon_and_replica
    (auto / "warm" / "team.md").write_text("# Team\n- 旧说法\n", encoding="utf-8")
    (hb / "warm" / "team.md").write_text("# Team replica\n", encoding="utf-8")

    ok = mcp._apply_replace(hb, "warm/team.md", "- 旧说法", "- 新说法", TS)

    assert ok is True
    canon_text = (auto / "warm" / "team.md").read_text(encoding="utf-8")
    assert "- 新说法" in canon_text and "- 旧说法" not in canon_text
    assert (hb / "warm" / "team.md").read_text(encoding="utf-8") == "# Team replica\n"


def test_warm_redirect_seeds_replica_only_file(canon_and_replica, capsys):
    """~10 warm files exist only in the replica; without seeding, the canon
    exists() check would silently drop every update to them."""
    auto, hb = canon_and_replica
    (hb / "warm" / "only_here.md").write_text("# Only\n- 旧内容\n", encoding="utf-8")

    ok = mcp._apply_update(hb, "warm/only_here.md", "追加一条", TS)

    assert ok is True
    canon_text = (auto / "warm" / "only_here.md").read_text(encoding="utf-8")
    assert "旧内容" in canon_text          # seeded from the replica first
    assert "追加一条" in canon_text        # then the update landed
    assert (hb / "warm" / "only_here.md").read_text(encoding="utf-8") == "# Only\n- 旧内容\n"
    assert "seeded canonical copy from replica" in capsys.readouterr().err


def test_non_warm_tiers_stay_on_heartbeat(canon_and_replica):
    auto, hb = canon_and_replica
    (hb / "system" / "engineering_roadmap.md").write_text("# Roadmap\n", encoding="utf-8")

    ok = mcp._apply_update(hb, "system/engineering_roadmap.md", "新条目", TS)

    assert ok is True
    assert "新条目" in (hb / "system" / "engineering_roadmap.md").read_text(encoding="utf-8")
    assert not (auto / "system" / "engineering_roadmap.md").exists()


def test_warm_not_redirected_outside_heartbeat_dir(canon_and_replica, tmp_path):
    """A session running against any OTHER memory dir writes warm/ in place."""
    auto, _hb = canon_and_replica
    other = _make_tree(tmp_path, "other")
    (other / "warm" / "team.md").write_text("# Team\n", encoding="utf-8")

    ok = mcp._apply_update(other, "warm/team.md", "本地写入", TS)

    assert ok is True
    assert "本地写入" in (other / "warm" / "team.md").read_text(encoding="utf-8")
    assert not (auto / "warm" / "team.md").exists()


def test_warm_missing_in_both_dirs_still_skipped(canon_and_replica, capsys):
    _auto, hb = canon_and_replica
    ok = mcp._apply_update(hb, "warm/ghost.md", "内容", TS)
    assert ok is False
    assert "does not exist" in capsys.readouterr().err


# ── [12] 2026-07-09: warm/ reroute in memory_daily_post ──────────────────


def test_daily_warm_update_under_heartbeat_lands_in_canon(canon_and_replica, capsys):
    """memory-daily emits the same '→ UPDATE:' directives as consolidate; its
    in-place replica write was destroyed by the next newer-wins tidy sync."""
    auto, hb = canon_and_replica
    (auto / "warm" / "team.md").write_text("# Team canon\n", encoding="utf-8")
    replica_before = "# Team replica\n"
    (hb / "warm" / "team.md").write_text(replica_before, encoding="utf-8")

    mdp._apply_update(hb, "warm/team.md", "每日更新一条", TS)

    assert "每日更新一条" in (auto / "warm" / "team.md").read_text(encoding="utf-8")
    # tidy's sync stays the replica's ONLY writer
    assert (hb / "warm" / "team.md").read_text(encoding="utf-8") == replica_before
    err = capsys.readouterr().err
    assert "[memory-daily]" in err and "rerouted to canonical" in err


def test_daily_warm_seeds_replica_only_file(canon_and_replica):
    auto, hb = canon_and_replica
    (hb / "warm" / "only_here.md").write_text("# Only\n- 旧内容\n", encoding="utf-8")

    mdp._apply_update(hb, "warm/only_here.md", "追加一条", TS)

    canon_text = (auto / "warm" / "only_here.md").read_text(encoding="utf-8")
    assert "旧内容" in canon_text and "追加一条" in canon_text
    assert (hb / "warm" / "only_here.md").read_text(encoding="utf-8") == "# Only\n- 旧内容\n"


def test_daily_non_warm_stays_on_heartbeat(canon_and_replica):
    auto, hb = canon_and_replica
    (hb / "system" / "todos.md").write_text("# Todos\n", encoding="utf-8")

    mdp._apply_update(hb, "system/todos.md", "新条目", TS)

    assert "新条目" in (hb / "system" / "todos.md").read_text(encoding="utf-8")
    assert not (auto / "system" / "todos.md").exists()


def test_daily_warm_not_redirected_outside_heartbeat_dir(canon_and_replica, tmp_path):
    auto, _hb = canon_and_replica
    other = _make_tree(tmp_path, "other_daily")
    (other / "warm" / "team.md").write_text("# Team\n", encoding="utf-8")

    mdp._apply_update(other, "warm/team.md", "本地写入", TS)

    assert "本地写入" in (other / "warm" / "team.md").read_text(encoding="utf-8")
    assert not (auto / "warm" / "team.md").exists()


# ── [12] 2026-07-09: divergence guard in the tidy newer-wins sync ─────────


@pytest.fixture
def tidy_pair(tmp_path, monkeypatch):
    """Fake canon (auto) + replica (heartbeat) pair for memory_tidy_post."""
    auto = _make_tree(tmp_path, "tidy_auto")
    hb = _make_tree(tmp_path, "tidy_heartbeat")
    monkeypatch.setattr(mtp, "AUTO_MEMORY", auto)
    monkeypatch.setattr(mtp, "HEARTBEAT_MEMORY", hb)
    return auto, hb


def _make_src_newer(src: Path, dst: Path):
    now = time.time()
    os.utime(dst, (now - 300, now - 300))
    os.utime(src, (now, now))


def test_tidy_sync_skips_replica_with_unique_auto_update(tidy_pair, capsys):
    """The destruction scenario: auto is newer, but the replica carries an
    auto-update block auto never saw — overwriting would silently destroy it."""
    auto, hb = tidy_pair
    src = auto / "warm" / "team.md"
    dst = hb / "warm" / "team.md"
    src.write_text("# Team\n- 主目录新内容\n", encoding="utf-8")
    replica = ("# Team\n\n<!-- auto-update 2026-07-08 -->\n- 只在副本里的更新\n")
    dst.write_text(replica, encoding="utf-8")
    _make_src_newer(src, dst)

    mtp._sync_warm_auto_to_heartbeat()

    assert dst.read_text(encoding="utf-8") == replica  # NOT overwritten
    err = capsys.readouterr().err
    assert "NOT syncing warm/team.md" in err and "auto-update" in err


def test_tidy_sync_overwrites_when_replica_blocks_all_in_canon(tidy_pair, capsys):
    auto, hb = tidy_pair
    shared_block = "<!-- auto-update 2026-07-07 -->\n- 两边都有的更新\n"
    src = auto / "warm" / "team.md"
    dst = hb / "warm" / "team.md"
    src.write_text(f"# Team\n\n{shared_block}\n- 主目录另有新内容\n", encoding="utf-8")
    dst.write_text(f"# Team\n\n{shared_block}", encoding="utf-8")
    _make_src_newer(src, dst)

    mtp._sync_warm_auto_to_heartbeat()

    assert "主目录另有新内容" in dst.read_text(encoding="utf-8")
    assert "synced auto→heartbeat warm/: team.md" in capsys.readouterr().err


def test_tidy_sync_replica_newer_still_skipped(tidy_pair, capsys):
    """Pre-existing newer-wins rule unchanged: replica newer → no write."""
    auto, hb = tidy_pair
    src = auto / "warm" / "team.md"
    dst = hb / "warm" / "team.md"
    src.write_text("# Team 旧\n", encoding="utf-8")
    replica = "# Team 新\n"
    dst.write_text(replica, encoding="utf-8")
    _make_src_newer(dst, src)  # replica newer

    mtp._sync_warm_auto_to_heartbeat()

    assert dst.read_text(encoding="utf-8") == replica


def test_tidy_root_feedback_sync_skips_diverged_replica(tidy_pair, capsys):
    """Root feedback files have no directive reroute, so the same guard
    protects them from the newer-wins overwrite."""
    auto, hb = tidy_pair
    src = auto / "feedback_x.md"
    dst = hb / "feedback_x.md"
    src.write_text("# 规则\n- 主目录版本\n", encoding="utf-8")
    replica = "# 规则\n\n<!-- auto-update 2026-07-08 -->\n- 只在副本里的规则\n"
    dst.write_text(replica, encoding="utf-8")
    _make_src_newer(src, dst)

    mtp._sync_root_feedback_auto_to_heartbeat()

    assert dst.read_text(encoding="utf-8") == replica
    assert "NOT syncing feedback_x.md" in capsys.readouterr().err


def test_replica_only_update_blocks_extraction():
    src = "# F\n<!-- auto-update 2026-07-01 -->\n- 共同\n"
    dst = ("# F\n<!-- auto-update 2026-07-01 -->\n- 共同\n"
           "<!-- auto-update 2026-07-08 -->\n- 独有\n")
    missing = mtp._replica_only_update_blocks(src, dst)
    assert len(missing) == 1
    assert "独有" in missing[0]
    assert mtp._replica_only_update_blocks(dst, dst) == []


# ── F22: exact-slug memory-dir binding in session_compact ────────────────

_SESSION_COMPACT = Path(__file__).resolve().parents[3] / "scripts" / "session_compact.py"

_needs_session_compact = pytest.mark.skipif(
    not _SESSION_COMPACT.exists(),
    reason="scripts/session_compact.py not present")


def _load_session_compact():
    spec = importlib.util.spec_from_file_location("session_compact_under_test",
                                                  _SESSION_COMPACT)
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    return sc


@_needs_session_compact
def test_session_compact_binds_memory_dirs_by_exact_slug():
    sc = _load_session_compact()

    # The regression: the repo slug also ends with "-jarvis", so suffix
    # matching bound BOTH names to the heartbeat dir and jarvis-root was
    # never compacted.  Verify the two dirs differ and match their expected
    # slugs (derived, not hardcoded).
    from core.claude_projects import path_slug, JARVIS_DIR
    expected_auto_slug = path_slug(JARVIS_DIR.parents[1])
    expected_hb_slug = path_slug(JARVIS_DIR)
    assert sc.AUTO_MEMORY.parent.name == expected_auto_slug
    assert sc.HEARTBEAT_MEMORY.parent.name == expected_hb_slug
    assert sc.AUTO_MEMORY != sc.HEARTBEAT_MEMORY


# ── [15] 2026-07-09: compact vs concurrent-append race guard ──────────────


def _racing_reader(sc, monkeypatch, filename: str, appended: str):
    """Wrap sc.read_file so the target file gets a concurrent append right
    after the compact function reads it — the F2-symptom race."""
    orig_read = sc.read_file

    def racing_read(path):
        text = orig_read(path)
        if path.name == filename:
            with open(path, "a", encoding="utf-8") as f:
                f.write(appended)
        return text

    monkeypatch.setattr(sc, "read_file", racing_read)


@_needs_session_compact
def test_compact_hourly_log_skips_on_concurrent_append(tmp_path, monkeypatch):
    sc = _load_session_compact()
    mem = tmp_path / "mem"
    (mem / "timeline").mkdir(parents=True)
    hourly = mem / "timeline" / "hourly_log.md"
    original = (f"### 2020-01-01 03:00\n- 旧条目\n"
                f"### {sc.TODAY} 00:00\n- 新条目\n")
    hourly.write_text(original, encoding="utf-8")
    appended = f"### {sc.TODAY} 01:00\n- 并发追加\n"
    _racing_reader(sc, monkeypatch, "hourly_log.md", appended)

    result = sc.compact_hourly_log(mem)

    assert result.get("skipped") == "concurrent_write"
    # The concurrent append survived and nothing was rewritten or archived.
    assert hourly.read_text(encoding="utf-8") == original + appended
    assert not (mem / "hourly_archive.md").exists()


@_needs_session_compact
def test_compact_hourly_log_normal_run_still_archives(tmp_path):
    sc = _load_session_compact()
    mem = tmp_path / "mem"
    (mem / "timeline").mkdir(parents=True)
    hourly = mem / "timeline" / "hourly_log.md"
    hourly.write_text(f"### 2020-01-01 03:00\n- 旧条目\n"
                      f"### {sc.TODAY} 00:00\n- 新条目\n", encoding="utf-8")

    result = sc.compact_hourly_log(mem)

    assert result == {"archived": 1, "kept": 1}
    kept = hourly.read_text(encoding="utf-8")
    assert "新条目" in kept and "旧条目" not in kept
    assert "旧条目" in (mem / "hourly_archive.md").read_text(encoding="utf-8")


@_needs_session_compact
def test_compact_daily_log_skips_on_concurrent_append(tmp_path, monkeypatch):
    sc = _load_session_compact()
    mem = tmp_path / "mem"
    mem.mkdir()
    daily = mem / "daily_log.md"
    original = f"## 2020-01-01\n- 旧条目\n## {sc.TODAY}\n- 新条目\n"
    daily.write_text(original, encoding="utf-8")
    appended = f"## {sc.TODAY}\n- 并发追加\n"
    _racing_reader(sc, monkeypatch, "daily_log.md", appended)

    result = sc.compact_daily_log(mem)

    assert result.get("skipped") == "concurrent_write"
    assert daily.read_text(encoding="utf-8") == original + appended
    assert not (mem / "daily_archive.md").exists()


# ── REQ-93 (2026-07-14): resolved issue files auto-archive from system/ ────


def _aged_system_file(root: Path, name: str, body: str, age_days: float = 10):
    sys_dir = root / "system"
    sys_dir.mkdir(parents=True, exist_ok=True)
    f = sys_dir / name
    f.write_text(body, encoding="utf-8")
    old = time.time() - age_days * 86400
    os.utime(f, (old, old))
    return f


def test_resolved_issue_archived(tmp_path, monkeypatch):
    monkeypatch.setattr(mtp, "MEMORY_DIR", tmp_path)
    f = _aged_system_file(
        tmp_path, "issue_foo.md",
        "---\nname: issue_foo\nstatus: fixed\n---\n\n# fixed issue\n")
    mtp._archive_resolved_system_issues()
    assert not f.exists()
    dest = tmp_path / "archive" / "system" / "issue_foo.md"
    assert dest.exists() and "fixed issue" in dest.read_text()


def test_resolved_variants_and_survivors(tmp_path, monkeypatch):
    monkeypatch.setattr(mtp, "MEMORY_DIR", tmp_path)
    gone = _aged_system_file(
        tmp_path, "issue_a.md",
        "---\nstatus: fixed-uncommitted\n---\nbody\n")
    open_issue = _aged_system_file(
        tmp_path, "issue_b.md", "---\nstatus: open\n---\nbody\n")
    no_frontmatter = _aged_system_file(
        tmp_path, "todos.md", "# 进行中\n- stuff\n")
    fresh_fixed = _aged_system_file(
        tmp_path, "issue_c.md", "---\nstatus: fixed\n---\nbody\n",
        age_days=2)
    mtp._archive_resolved_system_issues()
    assert not gone.exists()
    assert open_issue.exists()
    assert no_frontmatter.exists()
    assert fresh_fixed.exists()  # <7 days: stays visible for prod verification


def test_resolved_status_in_body_not_frontmatter_stays(tmp_path, monkeypatch):
    monkeypatch.setattr(mtp, "MEMORY_DIR", tmp_path)
    f = _aged_system_file(
        tmp_path, "notes.md",
        "# notes (no frontmatter)\n\n---\nstatus: fixed\n---\nquoted block\n")
    mtp._archive_resolved_system_issues()
    assert f.exists()  # file doesn't START with frontmatter → operator-owned


def test_resolved_symlink_never_touched(tmp_path, monkeypatch):
    monkeypatch.setattr(mtp, "MEMORY_DIR", tmp_path)
    target = tmp_path / "canonical.md"
    target.write_text("---\nstatus: fixed\n---\nbody\n")
    old = time.time() - 10 * 86400
    os.utime(target, (old, old))
    sys_dir = tmp_path / "system"
    sys_dir.mkdir(parents=True, exist_ok=True)
    link = sys_dir / "open_threads.md"
    link.symlink_to(target)
    mtp._archive_resolved_system_issues()
    assert link.is_symlink() and target.exists()


def test_resolved_archive_name_collision_gets_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(mtp, "MEMORY_DIR", tmp_path)
    archive_dir = tmp_path / "archive" / "system"
    archive_dir.mkdir(parents=True)
    (archive_dir / "issue_dup.md").write_text("earlier archived copy")
    _aged_system_file(tmp_path, "issue_dup.md",
                      "---\nstatus: closed\n---\nnew copy\n")
    mtp._archive_resolved_system_issues()
    copies = sorted(archive_dir.glob("issue_dup*.md"))
    assert len(copies) == 2
    assert (archive_dir / "issue_dup.md").read_text() == "earlier archived copy"


def test_hr_opening_prose_file_not_treated_as_frontmatter(tmp_path, monkeypatch):
    # Red-team: a file that merely OPENS with a markdown horizontal rule and
    # later contains a 'status: fixed' prose line must stay operator-owned.
    monkeypatch.setattr(mtp, "MEMORY_DIR", tmp_path)
    f = _aged_system_file(
        tmp_path, "ops_notes.md",
        "---\n# ops notes\nsome prose line\nstatus: fixed\n---\nongoing section\n")
    mtp._archive_resolved_system_issues()
    assert f.exists()


def test_capitalized_status_still_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(mtp, "MEMORY_DIR", tmp_path)
    f = _aged_system_file(
        tmp_path, "issue_cap.md",
        "---\nname: issue_cap\nStatus: Fixed\n---\nbody\n")
    mtp._archive_resolved_system_issues()
    assert not f.exists()
