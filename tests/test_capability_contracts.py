"""Focused behavioral contracts for capability inventory evidence gaps."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_memory_weekly_hooks_gate_then_archive_and_replace_digest(
    tmp_path, monkeypatch, capsys,
):
    memory = tmp_path / "memory"
    timeline = memory / "timeline"
    timeline.mkdir(parents=True)
    daily = timeline / "daily_log.md"
    daily.write_text("\n".join(f"## 2026-08-{day:02d}\nentry {day}" for day in range(1, 6)))
    (timeline / "longterm_digest.md").write_text("old digest", encoding="utf-8")

    pre = subprocess.run(
        ["bash", str(ROOT / "tasks" / "memory_weekly_pre.sh")],
        env={"MEMORY_DIR": str(memory)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ready for consolidation" in pre.stdout
    assert "entry 5" in pre.stdout

    from tasks import memory_weekly_post

    monkeypatch.setattr(memory_weekly_post, "MEMORY_DIR", memory)
    monkeypatch.setattr(memory_weekly_post, "DAILY_LOG", daily)
    monkeypatch.setattr(memory_weekly_post, "DAILY_ARCHIVE", timeline / "daily_archive.md")
    monkeypatch.setattr(memory_weekly_post, "LONGTERM", timeline / "longterm_digest.md")
    monkeypatch.setattr(memory_weekly_post, "LONGTERM_BAK", timeline / "longterm_digest.bak.md")
    monkeypatch.setattr(memory_weekly_post, "now_local_str", lambda _fmt: "2026-08-12 18:00")
    monkeypatch.setattr(sys, "stdin", io.StringIO("durable weekly synthesis with enough detail"))

    assert memory_weekly_post.main() == 0
    assert daily.read_text(encoding="utf-8") == ""
    assert "entry 5" in (timeline / "daily_archive.md").read_text(encoding="utf-8")
    assert (timeline / "longterm_digest.bak.md").read_text(encoding="utf-8") == "old digest"
    digest = (timeline / "longterm_digest.md").read_text(encoding="utf-8")
    assert "Last updated: 2026-08-12 18:00" in digest
    assert "durable weekly synthesis" in digest
    assert "Weekly digest updated" in capsys.readouterr().err


def test_runtime_provider_cli_persists_preference_and_rejects_invalid(capsys):
    from core import runtime_provider

    assert runtime_provider.main(["set", "conversation-1", "codex"]) == 0
    assert capsys.readouterr().out.strip() == "codex"
    assert runtime_provider.main(["get", "conversation-1"]) == 0
    assert capsys.readouterr().out.strip() == "codex"
    assert "Codex 优先" in runtime_provider.preference_label("codex")
    with pytest.raises(ValueError, match="unsupported provider preference"):
        runtime_provider.set_preference("conversation-1", "unknown")


def test_usage_stats_cli_is_numeric_only_and_counts_subagent_tokens(
    tmp_path, monkeypatch, capsys,
):
    from core import usage_stats

    projects = tmp_path / "projects"
    main_dir = projects / "-Users-pascal-Desktop-jarvis"
    sub_dir = main_dir / "session" / "subagents"
    sub_dir.mkdir(parents=True)
    main_dir.mkdir(parents=True, exist_ok=True)

    def record(session_id: str, text: str, tokens: int) -> str:
        return json.dumps({
            "type": "assistant",
            "timestamp": "2026-08-12T10:00:00Z",
            "sessionId": session_id,
            "message": {
                "model": "claude-test",
                "content": text,
                "usage": {
                    "input_tokens": tokens,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 7,
                },
            },
        }) + "\n"

    (main_dir / "main.jsonl").write_text(
        record("main-session", "private main transcript text", 11), encoding="utf-8"
    )
    (sub_dir / "worker.jsonl").write_text(
        record("worker-session", "private subagent transcript text", 13), encoding="utf-8"
    )
    monkeypatch.setattr(usage_stats, "PROJECTS_DIR", projects)
    monkeypatch.setattr(usage_stats, "CACHE_PATH", tmp_path / "usage-cache.json")
    usage_stats._AGG_MEMO.update(sig=None, agg=None)

    assert usage_stats._cli(["--days", "1", "--rebuild"]) == 0
    output = capsys.readouterr().out
    assert "sessions" in output and "1" in output
    assert "subagent tokens" in output
    assert "private main transcript text" not in output
    assert "private subagent transcript text" not in output
    cache = (tmp_path / "usage-cache.json").read_text(encoding="utf-8")
    assert "private main transcript text" not in cache
    assert "private subagent transcript text" not in cache


def test_delegation_shadow_cli_classifies_without_persisting(capsys):
    from core import delegation_shadow

    assert delegation_shadow.main([
        "classify", "--text", "请把报告发给对方，发完核对消息记录",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["is_delegation"] is True
    assert payload["risk_tier"] >= 1


def test_eigenflux_skill_overlay_cli_writes_composed_contract(tmp_path):
    from core import eigenflux_skill_overlay

    base = tmp_path / "base.md"
    overlay = tmp_path / "overlay.md"
    output = tmp_path / "output.md"
    base.write_text("intro\n\n### Fetch Unread Messages\nbody", encoding="utf-8")
    overlay.write_text("Jarvis verified-send rule", encoding="utf-8")

    assert eigenflux_skill_overlay.main([
        "--base", str(base), "--overlay", str(overlay), "--output", str(output),
    ]) == 0
    rendered = output.read_text(encoding="utf-8")
    assert eigenflux_skill_overlay.BEGIN in rendered
    assert "Jarvis verified-send rule" in rendered
    assert rendered.index("Jarvis verified-send rule") < rendered.index(
        "### Fetch Unread Messages")


def test_admin_session_and_live_chat_contracts(tmp_path, monkeypatch):
    import admin

    project = tmp_path / "project"
    project.mkdir()
    tracker = tmp_path / "active_sessions.json"
    session_id = "session-safe-1"
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "user",
            "timestamp": "2026-08-12T10:00:00Z",
            "message": {"role": "user", "content": "hello from live chat"},
        }) + "\n",
        encoding="utf-8",
    )
    tracker.write_text(json.dumps({
        "ou_owner": {"session_id": session_id, "counter": 1},
    }), encoding="utf-8")
    monkeypatch.setattr(admin, "PROJECT_DIR", project)
    monkeypatch.setattr(admin, "SESSION_SEARCH_PATHS", [project])
    monkeypatch.setattr(admin, "SESSION_TRACKER", tracker)

    loaded = admin.load_full_session(session_id)
    assert loaded and "hello from live chat" in loaded[0]["text"]
    assert admin.load_full_session("../escape") == []
    live = admin.live_chat()
    assert live["session_id"] == session_id
    assert live["conv_key"] == "ou_owner"
    assert "hello from live chat" in live["messages"][0]["text"]
