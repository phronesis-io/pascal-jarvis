"""Tests for the perception layer MVP (docs/prd_perception_ingestion.md)."""

import json
import os
import subprocess
import time

from core.memory import load_tiered_memory
from core.perception import PerceptionRuntime, content_hash, parse_interval
from sources import file_watch, git_repo


def _runtime(tmp_path, sources_yaml: str) -> PerceptionRuntime:
    (tmp_path / "sources.yaml").write_text(sources_yaml)
    memory = tmp_path / "memory"
    (memory / "system").mkdir(parents=True)
    return PerceptionRuntime(tmp_path, memory)


# ── primitives ───────────────────────────────────────────────────────


def test_content_hash_is_stable_and_truncating():
    a = content_hash("t" * 200, "b" * 200)
    b = content_hash("t" * 80 + "DIFFERENT", "b" * 100 + "TAIL")
    assert a == b  # only title[:80] + body[:100] count (single formula §5.6)


def test_parse_interval():
    assert parse_interval("15m") == 900
    assert parse_interval("2h") == 7200
    assert parse_interval(600) == 600
    assert parse_interval("garbage") == 900  # default


# ── file_watch adapter ───────────────────────────────────────────────


def test_file_watch_baselines_then_detects_change(tmp_path):
    doc = tmp_path / "REPORT_x.md"
    doc.write_text("v1 content")
    cfg = {"globs": [str(tmp_path / "*REPORT*.md")]}

    signals, state = file_watch.collect(cfg, {})
    assert signals == []  # first run baselines silently — no flood

    time.sleep(0.02)
    doc.write_text("v2 content changed")
    os.utime(doc, (time.time() + 1, time.time() + 1))
    signals, state = file_watch.collect(cfg, state)
    assert len(signals) == 1
    assert "变更" in signals[0]["title"]
    assert "v2 content" in signals[0]["body"]

    # unchanged → silent
    signals, state = file_watch.collect(cfg, state)
    assert signals == []


def test_file_watch_never_raises_on_bad_glob():
    signals, state = file_watch.collect({"globs": ["/nonexistent/**/x.md"]}, {})
    assert signals == []


# ── git_repo adapter ─────────────────────────────────────────────────


def test_git_repo_signals_new_commits(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"],):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "feat: hello world"],
                   cwd=repo, env=env, check=True, capture_output=True)

    signals, state = git_repo.collect({"repos_dir": str(tmp_path)}, {})
    assert len(signals) == 1
    assert "hello world" in signals[0]["title"]
    assert signals[0]["event_id"].startswith("demo@")

    # Second run inside the 120s cursor overlap may re-emit the commit —
    # the adapter contract requires a STABLE event_id so the runtime
    # seen-store drops the duplicate (overlap-then-dedup by design).
    signals2, _ = git_repo.collect({"repos_dir": str(tmp_path)}, state)
    assert all(s["event_id"] == signals[0]["event_id"] for s in signals2)


def test_git_repo_exclude(tmp_path):
    (tmp_path / "skipme" / ".git").mkdir(parents=True)
    signals, state = git_repo.collect(
        {"repos_dir": str(tmp_path), "exclude": ["skipme"]}, {})
    assert signals == []
    assert state.get("error_type") is None


# ── runtime pipeline ─────────────────────────────────────────────────


SOURCES_FILEWATCH = """
perception:
  defaults: {sensitivity: internal, interval: 1s}
  sources:
    - id: docs
      type: file_watch
      collect: {globs: ["%s/*.md"]}
      schedule: {interval: 1s}
      perceive: {buffer: inbox_ops.md}
"""


def test_pipeline_end_to_end(tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    rt = _runtime(tmp_path, SOURCES_FILEWATCH % watched)

    # run 1: baseline
    summary = rt.run_collect()
    assert "collected=0" in summary

    (watched / "note.md").write_text("重要变更内容")
    time.sleep(1.1)
    summary = rt.run_collect()
    assert "collected=1" in summary

    inbox = (rt.system_dir / "inbox_ops.md").read_text()
    assert "新增: note.md" in inbox
    assert "重要变更内容" in inbox
    # seen-store recorded the delivery
    seen = (rt.system_dir / "perception_seen.jsonl").read_text()
    assert "deliver" in seen

    # run 3: same state — dedup, no double entry
    time.sleep(1.1)
    summary = rt.run_collect()
    assert "collected=0" in summary
    assert inbox.count("note.md") == (rt.system_dir / "inbox_ops.md").read_text().count("note.md")


def test_pipeline_dedup_by_event_id(tmp_path):
    rt = _runtime(tmp_path, SOURCES_FILEWATCH % tmp_path)
    # seed seen-store with an event, then hand-deliver a matching signal
    from core.jsonl import append_jsonl
    append_jsonl(rt.seen_file, {"event_id": "e1", "source_id": "docs",
                                "content_hash": "x", "action": "deliver",
                                "epoch": int(time.time()), "ts": "now"})
    primary, clusters = rt._load_seen()
    assert ("docs", "e1") in primary


def test_invalid_cfg_skips_source_but_not_pass(tmp_path):
    # A misconfigured metrics_probe (no command) is skipped with a note via
    # the validate_cfg hook; the valid file_watch source still runs.
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "pre.md").write_text("existing")
    rt = _runtime(tmp_path, f"""
perception:
  sources:
    - id: badprobe
      type: metrics_probe
      collect: {{name: nope}}
      schedule: {{interval: 1s}}
    - id: docs
      type: file_watch
      collect: {{globs: ["{watched}/*.md"]}}
      schedule: {{interval: 1s}}
""")
    summary = rt.run_collect()
    assert "errors=1" in summary
    assert "badprobe: config invalid" in summary
    # file_watch ran (baselined) despite the invalid sibling
    state = json.loads(rt.state_file.read_text())
    assert state["docs"]["adapter_state"]["baselined"] is True
    assert "badprobe" not in state


def test_validate_cfg_crash_does_not_kill_pass(tmp_path, monkeypatch):
    from sources import metrics_probe
    monkeypatch.setattr(metrics_probe, "validate_cfg",
                        lambda cfg: 1 / 0)
    rt = _runtime(tmp_path, """
perception:
  sources:
    - id: probe
      type: metrics_probe
      collect: {command: "echo x"}
      schedule: {interval: 1s}
""")
    summary = rt.run_collect()
    assert "errors=1" in summary and "validate_cfg crashed" in summary


def test_unknown_adapter_reports_error(tmp_path):
    rt = _runtime(tmp_path, """
perception:
  sources:
    - id: bad
      type: does_not_exist
      schedule: {interval: 1s}
""")
    summary = rt.run_collect()
    assert "errors=1" in summary
    assert "does_not_exist" in summary


def test_missing_sources_yaml_is_noop(tmp_path):
    memory = tmp_path / "memory"
    (memory / "system").mkdir(parents=True)
    rt = PerceptionRuntime(tmp_path, memory)
    assert rt.run_collect() == "no sources configured"


def test_inbox_retention_trim(tmp_path):
    # An UNCAPPED buffer (not in core.memory._SYSTEM_FILE_CAPS) keeps the
    # legacy 500-line retention rule.
    rt = _runtime(tmp_path, "perception: {sources: []}")
    inbox = rt.system_dir / "inbox_team.md"
    inbox.write_text("\n".join(f"line{i}" for i in range(600)) + "\n")
    rt._trim_inbox("inbox_team.md")
    kept = inbox.read_text().splitlines()
    assert len(kept) == 500 and kept[0] == "line100"
    # Overflow must land in warm/archive/ (loader skips it) — NOT top-level
    # warm/, which is auto-injected newest-first (2026-07-07 memory audit).
    archives = list((rt.memory_dir / "warm" / "archive").glob("perception_archive_*.md"))
    assert archives and "line0" in archives[0].read_text()
    assert not list((rt.memory_dir / "warm").glob("perception_archive_*.md"))


# ── REQ-92: capped buffers retained to the loader char-cap, entry-aware ──


def _entry_ts(days_ago: float) -> str:
    from datetime import datetime, timedelta
    return (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(
        timespec="seconds")


def _entry(i: int, body_chars: int = 200, days_ago: float = 4.0) -> str:
    # Default timestamp ~4 days old: outside the 48h protect window (so the
    # cap rule can trim it) but inside the 7-day age bound (not auto-expired).
    return (f"### evt{i} | src | who | {_entry_ts(days_ago)} | "
            f"internal | buffer\ntitle {i}\n{'x' * body_chars}\n\n")


def test_capped_inbox_trims_to_entry_boundary(tmp_path):
    from core.memory import _SYSTEM_FILE_CAPS
    cap = _SYSTEM_FILE_CAPS["inbox_ops.md"]
    rt = _runtime(tmp_path, "perception: {sources: []}")
    inbox = rt.system_dir / "inbox_ops.md"
    entries = [_entry(i) for i in range(80)]  # ~19k chars, well over cap
    inbox.write_text("".join(entries))
    rt._trim_inbox("inbox_ops.md")
    kept = inbox.read_text()
    assert len(kept) <= cap
    assert kept.startswith("### ")            # never a mid-entry head
    assert "evt79" in kept                    # newest survives
    assert "evt0" not in kept                 # oldest archived
    archives = list((rt.memory_dir / "warm" / "archive").glob("perception_archive_*.md"))
    assert archives and "evt0" in archives[0].read_text()


def test_capped_inbox_heals_legacy_fragment_head(tmp_path):
    from core.memory import _SYSTEM_FILE_CAPS
    cap = _SYSTEM_FILE_CAPS["inbox_ops.md"]
    rt = _runtime(tmp_path, "perception: {sources: []}")
    inbox = rt.system_dir / "inbox_ops.md"
    fragment = "orphan tail of a half-cut entry\nmore orphan lines\n\n"
    inbox.write_text(fragment + "".join(_entry(i) for i in range(80)))
    rt._trim_inbox("inbox_ops.md")
    kept = inbox.read_text()
    assert kept.startswith("### ")
    assert "orphan tail" not in kept
    archives = list((rt.memory_dir / "warm" / "archive").glob("perception_archive_*.md"))
    assert archives and "orphan tail" in archives[0].read_text()


def test_capped_inbox_under_cap_is_noop(tmp_path):
    rt = _runtime(tmp_path, "perception: {sources: []}")
    inbox = rt.system_dir / "inbox_ops.md"
    content = "".join(_entry(i) for i in range(3))
    inbox.write_text(content)
    rt._trim_inbox("inbox_ops.md")
    assert inbox.read_text() == content
    assert not list((rt.memory_dir / "warm" / "archive").glob("*.md"))


def test_capped_inbox_no_headers_still_bounded(tmp_path):
    # Entry format drift must not disable retention — raw tail-keep fallback.
    from core.memory import _SYSTEM_FILE_CAPS
    cap = _SYSTEM_FILE_CAPS["inbox_ops.md"]
    rt = _runtime(tmp_path, "perception: {sources: []}")
    inbox = rt.system_dir / "inbox_ops.md"
    inbox.write_text("\n".join(f"plain{i}" for i in range(2000)) + "\n")
    rt._trim_inbox("inbox_ops.md")
    kept = inbox.read_text()
    assert len(kept) <= cap
    assert kept.endswith("plain1999\n")       # tail kept, not head


def test_split_entries_keeps_oversized_newest_entry():
    from core.perception import split_entries_for_cap
    huge = _entry(1, body_chars=10)  # small old entry
    newest = _entry(2, body_chars=9000)  # alone exceeds cap
    keep, overflow = split_entries_for_cap(huge + newest, 8000)
    assert "evt2" in keep and keep.startswith("### evt2")
    assert overflow == huge


# ── sensitivity outbound view (PRD §3.4/§6 steps 1-2) ────────────────


def test_outbound_memory_skips_private_inbox(tmp_path):
    memory = tmp_path / "memory"
    (memory / "system").mkdir(parents=True)
    (memory / "system" / "inbox_team.md").write_text("团队动态")
    (memory / "system" / "inbox_private_mail.md").write_text("私密邮件内容")

    inbound = load_tiered_memory(memory)
    assert "私密邮件内容" in inbound and "团队动态" in inbound

    outbound = load_tiered_memory(memory, purpose="outbound")
    assert "私密邮件内容" not in outbound
    assert "团队动态" in outbound  # internal stays visible


# ── lark_mail adapter ────────────────────────────────────────────────


def _mail_msgs(*rows):
    return [{"date": d, "from": f, "subject": s, "message_id": m}
            for d, f, s, m in rows]


# The first-run lookback cursor (lark_mail.FIRST_RUN_LOOKBACK_H=24) is relative
# to wall-clock NOW, so a hardcoded recent date eventually falls behind the
# window and the tests flake the moment real time crosses that date + 24h
# (observed 2026-06-13). Use a timestamp a few hours ago so "recent" is ALWAYS
# inside the 24h window regardless of when the suite runs.
def _recent_mail_date(hours_ago: float = 2.0) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(_t.time() - hours_ago * 3600))


def test_lark_mail_collects_and_advances_cursor(monkeypatch):
    from sources import lark_mail
    calls = []

    recent = _recent_mail_date()

    def fake_triage(mailbox, folder):
        calls.append(mailbox)
        return _mail_msgs(
            (recent, "alice@x.com", "Hello", "MID1"),
            ("2020-01-01 00:00", "old@x.com", "Ancient", "MID0"),
        )

    monkeypatch.setattr(lark_mail, "_triage", fake_triage)
    signals, state = lark_mail.collect({"mailboxes": ["me"]}, {})
    assert calls == ["me"]
    # ancient message is behind the 24h first-run lookback cursor
    assert [s["event_id"] for s in signals] == ["MID1"]
    assert signals[0]["title"].startswith("📧 Hello")
    assert "alice@x.com" in signals[0]["summary"]
    # body is metadata-only: never ingests mail content
    assert "MID1" in signals[0]["body"] and "Hello" in signals[0]["body"]
    assert state["cursors"]["me"] == recent
    assert state["error_type"] is None

    # second run with same payload: cursor admits the boundary message
    # (stable event_id → runtime seen-store dedups; overlap-then-dedup)
    signals2, _ = lark_mail.collect({"mailboxes": ["me"]}, state)
    assert [s["event_id"] for s in signals2] == ["MID1"]


def test_lark_mail_exclude_from_filters_but_advances_cursor(monkeypatch):
    from sources import lark_mail
    recent = _recent_mail_date()
    monkeypatch.setattr(lark_mail, "_triage", lambda mb, f: _mail_msgs(
        (recent, "noreply-dmarc-support@google.com", "Report", "MIDD"),
    ))
    signals, state = lark_mail.collect(
        {"mailboxes": ["me"], "exclude_from": ["dmarc"]}, {})
    assert signals == []
    assert state["cursors"]["me"] == recent


def test_lark_mail_error_isolated_per_mailbox(monkeypatch):
    from sources import lark_mail

    def fake_triage(mailbox, folder):
        if mailbox == "broken@x.com":
            raise RuntimeError("network")
        return _mail_msgs((_recent_mail_date(), "a@x.com", "Hi", "MID9"))

    monkeypatch.setattr(lark_mail, "_triage", fake_triage)
    signals, state = lark_mail.collect(
        {"mailboxes": ["broken@x.com", "me"]}, {})
    assert [s["event_id"] for s in signals] == ["MID9"]
    assert state["error_type"] == "network"  # surfaced for error accounting
    assert "broken@x.com" not in state["cursors"]  # no fake cursor on failure


# ── imap_mail adapter (163) ──────────────────────────────────────────


def _imap_secret(tmp_path):
    p = tmp_path / "163.json"
    p.write_text(json.dumps({
        "email": "u@163.com", "imap_host": "imap.163.com",
        "imap_port": 993, "auth_code": "SECRET"}))
    return str(p)


def test_imap_missing_secret_never_raises():
    from sources import imap_mail
    signals, state = imap_mail.collect({"secret_file": "/nope/x.json"}, {})
    assert signals == []
    assert state["error_type"] == "no_secret"


def test_imap_first_run_then_incremental(tmp_path, monkeypatch):
    from sources import imap_mail
    cfg = {"secret_file": _imap_secret(tmp_path)}

    def fetch1(secret, folder, since_uid, first_run):
        assert first_run and since_uid == 0
        return ([{"uid": 10, "from": "szy <notifications@github.com>",
                  "subject": "PR #45", "date_iso": "2026-06-12T09:00:00+0800"}],
                10, 555)
    monkeypatch.setattr(imap_mail, "_fetch_new", fetch1)
    signals, state = imap_mail.collect(cfg, {})
    assert len(signals) == 1
    assert signals[0]["event_id"] == "imap:u@163.com:555:10"
    assert "正文未注入" in signals[0]["body"] and signals[0]["url"] == ""
    assert state == {"uidvalidity": 555, "last_uid": 10, "error_type": None}

    # incremental: only UID > last_uid surfaces, cursor advances
    def fetch2(secret, folder, since_uid, first_run):
        assert not first_run and since_uid == 10
        return ([{"uid": 12, "from": "bank@cmb.com", "subject": "账单",
                  "date_iso": "2026-06-12T10:00:00+0800"}], 12, 555)
    monkeypatch.setattr(imap_mail, "_fetch_new", fetch2)
    signals, state = imap_mail.collect(cfg, state)
    assert [s["payload"]["uid"] for s in signals] == [12]
    assert state["last_uid"] == 12


def test_imap_exclude_and_error_passthrough(tmp_path, monkeypatch):
    from sources import imap_mail
    cfg = {"secret_file": _imap_secret(tmp_path),
           "exclude_from": ["mailmaster@163.com"]}

    def fetch(secret, folder, since_uid, first_run):
        return ([{"uid": 5, "from": "mailmaster@163.com", "subject": "系统",
                  "date_iso": "2026-06-12T08:00:00+0800"},
                 {"uid": 6, "from": "real@x.com", "subject": "Hi",
                  "date_iso": "2026-06-12T08:01:00+0800"}], 6, 1)
    monkeypatch.setattr(imap_mail, "_fetch_new", fetch)
    signals, state = imap_mail.collect(cfg, {})
    assert [s["payload"]["uid"] for s in signals] == [6]  # mailmaster filtered
    assert state["last_uid"] == 6  # cursor still advances past excluded

    def boom(secret, folder, since_uid, first_run):
        raise RuntimeError("connect")
    monkeypatch.setattr(imap_mail, "_fetch_new", boom)
    signals, state = imap_mail.collect(cfg, {"last_uid": 6, "uidvalidity": 1})
    assert signals == [] and state["error_type"] == "connect"
    assert state["last_uid"] == 6  # prior cursor preserved on failure


def test_imap_uidvalidity_reset_resyncs(tmp_path, monkeypatch):
    from sources import imap_mail
    cfg = {"secret_file": _imap_secret(tmp_path)}

    def fetch(secret, folder, since_uid, first_run):
        return ([{"uid": 3, "from": "a@x.com", "subject": "x",
                  "date_iso": "2026-06-12T08:00:00+0800"}], 3, 999)
    monkeypatch.setattr(imap_mail, "_fetch_new", fetch)
    # prior uidvalidity 555 != new 999 → drop batch, resync cursor, flag reset
    signals, state = imap_mail.collect(cfg, {"uidvalidity": 555, "last_uid": 40})
    assert signals == []
    assert state["error_type"] == "uidvalidity_reset"
    assert state["uidvalidity"] == 999 and state["last_uid"] == 3


def test_imap_decode_hdr_handles_mime_and_garbage():
    from sources import imap_mail
    assert imap_mail._decode_hdr("=?utf-8?B?5oub5ZWG?=") == "招商"
    assert imap_mail._decode_hdr("plain text") == "plain text"
    assert imap_mail._decode_hdr("") == ""


def test_capped_inbox_protects_recent_entries_over_cap(tmp_path):
    # A burst of young entries must NOT be trimmed even over the cap —
    # mail-triage consumes the buffer at ≤15/cycle and would lose them.
    import time as _t
    from datetime import datetime
    rt = _runtime(tmp_path, "perception: {sources: []}")
    inbox = rt.system_dir / "inbox_private_mail.md"
    young_ts = datetime.now().astimezone().isoformat(timespec="seconds")
    old = "".join(_entry(i) for i in range(10))          # 2026-07-14 = old
    young = "".join(
        f"### young{i} | lark_mail | who | {young_ts} | private | buffer\n"
        f"title {i}\n{'y' * 400}\n\n" for i in range(40))  # ~18k, over 8k cap
    inbox.write_text(old + young)
    rt._trim_inbox("inbox_private_mail.md")
    kept = inbox.read_text()
    assert all(f"young{i}" in kept for i in range(40))   # every young survives
    assert "evt0" not in kept                            # old ones archived


def test_capped_inbox_expires_week_old_entries_even_under_cap(tmp_path):
    # PRD §5.4: entries older than INBOX_MAX_AGE_DAYS age out even when the
    # buffer is small — a quiet inbox must not carry months-dead signals.
    rt = _runtime(tmp_path, "perception: {sources: []}")
    inbox = rt.system_dir / "inbox_ops.md"
    inbox.write_text(_entry(1, days_ago=30) + _entry(2, days_ago=1))
    rt._trim_inbox("inbox_ops.md")
    kept = inbox.read_text()
    assert "evt2" in kept and "evt1" not in kept
    archives = list((rt.memory_dir / "warm" / "archive").glob("perception_archive_*.md"))
    assert archives and "evt1" in archives[0].read_text()


# ── dry run (connector setup aid) ────────────────────────────────────


def test_dry_run_persists_nothing(tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "a.md").write_text("hello")
    rt = _runtime(tmp_path, SOURCES_FILEWATCH % watched)
    report = rt.dry_run()
    assert "docs (file_watch): ✓" in report
    assert not rt.state_file.exists()          # no state written
    assert not (rt.system_dir / "inbox_ops.md").exists()
    assert not rt.seen_file.exists()


def test_dry_run_reports_invalid_cfg_and_single_source(tmp_path):
    rt = _runtime(tmp_path, """
perception:
  sources:
    - id: badprobe
      type: metrics_probe
      collect: {name: nope}
    - id: probe
      type: metrics_probe
      collect: {name: ok, command: "echo '{\\"metrics\\": {\\"a\\": 1}}'", snapshot_hour: 0}
""")
    report = rt.dry_run()
    assert "badprobe (metrics_probe): ✗ config invalid" in report
    assert "probe (metrics_probe): ✓ 1 signal(s)" in report
    only = rt.dry_run("badprobe")
    assert "probe (metrics_probe): ✓" not in only
    assert rt.dry_run("ghost") == "source 'ghost' not found in sources.yaml"


def test_dry_run_metrics_probe_writes_no_history(tmp_path):
    hist = tmp_path / "h.jsonl"
    rt = _runtime(tmp_path, f"""
perception:
  sources:
    - id: probe
      type: metrics_probe
      collect:
        name: ok
        command: "echo '{{\\"metrics\\": {{\\"a\\": 1}}}}'"
        snapshot_hour: 0
        history_file: "{hist}"
""")
    report = rt.dry_run()
    assert "✓ 1 signal(s)" in report
    assert not hist.exists()
