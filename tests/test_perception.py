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
    rt = _runtime(tmp_path, "perception: {sources: []}")
    inbox = rt.system_dir / "inbox_ops.md"
    inbox.write_text("\n".join(f"line{i}" for i in range(600)) + "\n")
    rt._trim_inbox("inbox_ops.md")
    kept = inbox.read_text().splitlines()
    assert len(kept) == 500 and kept[0] == "line100"
    archives = list((rt.memory_dir / "warm").glob("perception_archive_*.md"))
    assert archives and "line0" in archives[0].read_text()


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
