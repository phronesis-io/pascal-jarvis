"""Regression tests for the 2026-06-11 interaction-quality P0 batch.

Covers docs/prd_interaction_quality.md REQ-02/03/04/05:
- run_cycle cross-process mutex (heartbeat_state.lock flock)
- error gate catching header-wrapped API failures
- outbox content dedup window
- interval override sidecar (W3.1 adaptive frequency)
"""

import fcntl
import io
import json
import sys
import time

from core.heartbeat import HeartbeatRunner
from core.heartbeat_loop import DEDUP_WINDOW_SECONDS, _is_duplicate_send
from core.safety import looks_like_error


def _make_runner(tmp_path, heartbeat_content: str, **kwargs) -> HeartbeatRunner:
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(heartbeat_content)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir(exist_ok=True)
    kwargs.setdefault("idle_judge", False)
    return HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=hb,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        model="sonnet",
        **kwargs,
    )


# ── REQ-02: run_cycle cross-process mutex ────────────────────────────


def test_run_cycle_skips_when_lock_held(tmp_path, monkeypatch):
    """A second concurrent run_cycle must skip, not race on state."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    called = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called.append(p) or "HEARTBEAT_OK")

    lock_path = runner.state_file.with_suffix(".lock")
    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = runner.run_cycle(force=True)

    assert result == ""
    assert called == []  # never reached Claude while the lock was held


def test_run_cycle_runs_after_lock_released(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    called = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)
    assert len(called) == 1


# ── REQ-03: header-wrapped API errors must not pass the gate ─────────


def test_wrapped_403_error_is_caught():
    # Exact shape that reached the user 7 times on 2026-06-10
    text = "**Intent** | Failed to authenticate. API Error: 403 Request not allowed"
    assert looks_like_error(text, proactive=True)


def test_wrapped_error_with_emoji_header_is_caught():
    text = "**🎯 Intent**\n\nFailed to authenticate. API Error: 403 Request not allowed"
    assert looks_like_error(text, proactive=True)


def test_legitimate_message_mentioning_api_late_is_not_flagged():
    text = ("今晚 Doctor Stretch 20:00 开始。课后我会给你一条闭环卡，"
            "记得记录今天拉到的位置和感受，回来按卡片提示回复就行。")
    assert not looks_like_error(text, proactive=True)


def test_interactive_reply_quoting_api_error_passes():
    # Pascal debugging this very bot: the reply legitimately quotes the error.
    text = '你日志里的 "API Error: 403 Request not allowed" 是认证层返回的，原因是…'
    assert not looks_like_error(text)            # interactive path
    assert looks_like_error(text, proactive=True)  # proactive gate stays strict


# ── REQ-04: outbox content dedup ─────────────────────────────────────


def _write_outbox_entry(jarvis_dir, text, ts_epoch):
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_epoch))
    entry = {"role": "assistant", "text": text, "ts": ts, "source": "heartbeat"}
    with open(jarvis_dir / "heartbeat_outbox.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def test_identical_message_within_window_is_duplicate(tmp_path):
    _write_outbox_entry(tmp_path, "同一条报错卡", time.time() - 3600)
    assert _is_duplicate_send("同一条报错卡", tmp_path)


def test_identical_message_outside_window_is_not_duplicate(tmp_path):
    _write_outbox_entry(tmp_path, "同一条报错卡", time.time() - DEDUP_WINDOW_SECONDS - 3600)
    assert not _is_duplicate_send("同一条报错卡", tmp_path)


def test_different_message_is_not_duplicate(tmp_path):
    _write_outbox_entry(tmp_path, "昨天的内容", time.time() - 60)
    assert not _is_duplicate_send("今天的新内容", tmp_path)


def test_no_outbox_file_is_not_duplicate(tmp_path):
    assert not _is_duplicate_send("任何内容", tmp_path)


# ── REQ-05: interval override sidecar (W3.1) ─────────────────────────


def test_interval_override_suppresses_due_task(tmp_path, monkeypatch):
    """Task due under the default interval must NOT run when the override
    interval (longer) says it isn't due yet."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    # last run 1.5h ago: due under 1h default, not due under 4h override
    runner.save_state({"t": {"last_run": int(time.time()) - 5400}})
    (runner.jarvis_dir / "interval_overrides.json").write_text(
        json.dumps({"t": 4 * 3600}))
    called = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=False)
    assert called == []

    # Without the override the same state must trigger the task
    (runner.jarvis_dir / "interval_overrides.json").unlink()
    runner.run_cycle(force=False)
    assert len(called) == 1


def test_load_interval_overrides_ignores_garbage(tmp_path):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    f = runner.jarvis_dir / "interval_overrides.json"

    assert runner.load_interval_overrides() == {}  # missing file
    f.write_text("not json")
    assert runner.load_interval_overrides() == {}
    f.write_text(json.dumps({"t": "soon"}))
    assert runner.load_interval_overrides() == {}
    f.write_text("[]")
    assert runner.load_interval_overrides() == {}
    f.write_text('{"t": 1e309}')
    assert runner.load_interval_overrides() == {}
    f.write_text(json.dumps({"t": 0, "u": 7200}))
    assert runner.load_interval_overrides() == {"u": 7200}


def test_apply_adaptations_writes_sidecar_with_chinese_keyword(tmp_path, monkeypatch):
    """'降频' — the exact word the insights used for two months while the old
    keyword list only knew 降低/减少 — must now take effect, into the sidecar
    file rather than heartbeat_state.json (where run_cycle clobbered it)."""
    (tmp_path / "HEARTBEAT.md").write_text(
        "### free-time-nudge\n- interval: 1h\n- prompt: hi\n")
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))

    from tasks.engagement_analyze_post import _apply_adaptations
    _apply_adaptations([
        {"target": "free-time-nudge", "suggestion": "建议降频 50%（0% engagement）"},
        {"target": "unknown-task", "suggestion": "建议降频"},
    ])

    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides == {"free-time-nudge": 7200}

    # Immediate repeat is blocked by the recompound cooldown (a few repeated
    # suggestions on the same data must not geometrically run to the clamp)
    _apply_adaptations([
        {"target": "free-time-nudge", "suggestion": "reduce frequency further"},
    ])
    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides == {"free-time-nudge": 7200}

    # After the cooldown expires, the suggestion compounds from the override
    meta_file = tmp_path / "interval_overrides_meta.json"
    meta = json.loads(meta_file.read_text())
    meta["free-time-nudge"] -= 4 * 86400
    meta_file.write_text(json.dumps(meta))
    _apply_adaptations([
        {"target": "free-time-nudge", "suggestion": "reduce frequency further"},
    ])
    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides == {"free-time-nudge": 14400}


def test_apply_adaptations_guardrails(tmp_path, monkeypatch):
    """Infra tasks are exempt from tuning; drift is capped at 4x base.
    (2026-06-11: the analyzer slowed Tier-0 calendar-sync to 2h in production
    within hours of the apply path going live.)"""
    (tmp_path / "HEARTBEAT.md").write_text(
        "### calendar-sync\n- interval: 30m\n- prompt: p\n\n"
        "### chatty-task\n- interval: 1h\n- prompt: q\n")
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    from tasks.engagement_analyze_post import _apply_adaptations

    # protected: calendar-sync is TIER0/PRIORITY — suggestion ignored
    _apply_adaptations([{"target": "calendar-sync", "suggestion": "降频"}])
    assert not (tmp_path / "interval_overrides.json").exists()

    # cap: repeated reductions stop at 4x the HEARTBEAT.md base (1h → 4h max)
    meta_file = tmp_path / "interval_overrides_meta.json"
    for _ in range(5):
        _apply_adaptations([{"target": "chatty-task", "suggestion": "reduce"}])
        if meta_file.exists():  # defeat the recompound cooldown for the test
            meta = json.loads(meta_file.read_text())
            meta["chatty-task"] = 0
            meta_file.write_text(json.dumps(meta))
    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides["chatty-task"] == 4 * 3600


def test_protected_preinstall_override_is_repaired_and_never_recreated(
        tmp_path, monkeypatch):
    (tmp_path / "HEARTBEAT.md").write_text(
        "### eigenflux-preinstall\n- interval: 24h\n- prompt: p\n\n"
        "### content-recommend\n- interval: 4h\n- prompt: q\n")
    (tmp_path / "interval_overrides.json").write_text(json.dumps({
        "eigenflux-preinstall": 48 * 3600,
        "content-recommend": 8 * 3600,
    }))
    (tmp_path / "interval_overrides_meta.json").write_text(json.dumps({
        "eigenflux-preinstall": 1,
        "content-recommend": 2,
    }))
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))

    from tasks.engagement_analyze_post import _apply_adaptations
    _apply_adaptations([{
        "target": "eigenflux-preinstall",
        "direction": "reduce",
        "suggestion": "低互动，继续降频",
    }])

    assert json.loads((tmp_path / "interval_overrides.json").read_text()) == {
        "content-recommend": 8 * 3600,
    }
    assert json.loads((tmp_path / "interval_overrides_meta.json").read_text()) == {
        "content-recommend": 2,
    }


def test_interval_parser_rejects_protected_infrastructure_overrides():
    from core.interval_config import (
        parse_interval_overrides,
        resolve_effective_interval,
    )

    assert parse_interval_overrides({
        "eigenflux-preinstall": 48 * 3600,
        "eigenflux-feed-triage": 2400,
    }) == {"eigenflux-feed-triage": 2400}
    assert resolve_effective_interval(
        "eigenflux-preinstall",
        24 * 3600,
        96 * 3600,
        {"eigenflux-preinstall": 48 * 3600},
    ) == 24 * 3600


def test_apply_adaptations_structured_direction(tmp_path, monkeypatch):
    """The analyzer's structured 'direction' field is authoritative —
    keyword parsing of the prose suggestion is only the legacy fallback
    (去关键词化, 2026-06-12)."""
    (tmp_path / "HEARTBEAT.md").write_text(
        "### a\n- interval: 1h\n- prompt: p\n\n### b\n- interval: 1h\n- prompt: q\n")
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    from tasks.engagement_analyze_post import _apply_adaptations

    _apply_adaptations([
        # direction wins even when the prose contains no keyword at all
        {"target": "a", "direction": "reduce", "suggestion": "信噪比不佳"},
        # explicit keep blocks the keyword fallback ("reduce" in prose)
        {"target": "b", "direction": "keep", "suggestion": "could reduce later"},
    ])
    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides == {"a": 7200}


def test_apply_adaptations_increase_direction(tmp_path, monkeypatch):
    """The 'increase' direction halves the interval (more frequent).
    Also verifies Chinese synonyms (加频/提高/增加) and the 0.25x floor."""
    (tmp_path / "HEARTBEAT.md").write_text(
        "### checkin\n- interval: 2h\n- prompt: p\n\n"
        "### content-recommend\n- interval: 4h\n- prompt: q\n\n"
        "### slow-task\n- interval: 30m\n- prompt: r\n")
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    from tasks.engagement_analyze_post import _apply_adaptations

    _apply_adaptations([
        {"target": "checkin", "direction": "increase", "suggestion": "用户互动频繁"},
        {"target": "content-recommend", "direction": "加频", "suggestion": "回复率上升"},
    ])

    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides["checkin"] == 3600         # 2h * 0.5 = 1h
    assert overrides["content-recommend"] == 7200  # 4h * 0.5 = 2h

    # Compound after cooldown: 1h * 0.5 = 30min, still above 0.25x base (2h/4=30min)
    meta_file = tmp_path / "interval_overrides_meta.json"
    meta = json.loads(meta_file.read_text())
    for k in meta:
        meta[k] = 0
    meta_file.write_text(json.dumps(meta))

    _apply_adaptations([
        {"target": "checkin", "direction": "increase", "suggestion": ""},
    ])
    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides["checkin"] == 1800  # 1h * 0.5 = 30min = 2h/4 (at floor)

    # Floor: a further increase cannot go below 0.25x base
    meta = json.loads(meta_file.read_text())
    for k in meta:
        meta[k] = 0
    meta_file.write_text(json.dumps(meta))
    _apply_adaptations([
        {"target": "checkin", "direction": "increase", "suggestion": ""},
    ])
    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides["checkin"] == 1800  # clamped at 0.25x base (2h/4=1800s)

    # Keyword fallback for "increase" (no structured direction)
    meta = json.loads(meta_file.read_text())
    for k in meta:
        meta[k] = 0
    meta_file.write_text(json.dumps(meta))
    _apply_adaptations([
        {"target": "slow-task", "suggestion": "increase frequency, more relevant"},
    ])
    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides["slow-task"] == 900  # 30m * 0.5 = 15m


def test_engagement_content_mix_written_to_memory(tmp_path, monkeypatch):
    from tasks import engagement_analyze_post as post
    monkeypatch.setattr(post, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(
        post, "CONTENT_MIX_FILE",
        tmp_path / "memory" / "system" / "engagement_content_mix.md",
    )

    post._write_content_mix([
        {"target": "checkin", "mode": "wellbeing", "weight": "decrease",
         "rationale": "ignored 80% in odd-hour wellbeing prompts"},
        {"target": "content-recommend", "mode": "philosophy longform",
         "weight": "increase", "rationale": "late replies clustered here"},
        {"target": "", "mode": "bad", "weight": "increase"},
    ])

    out = tmp_path / "memory" / "system" / "engagement_content_mix.md"
    text = out.read_text(encoding="utf-8")
    assert "# Engagement Content Mix" in text
    assert "**checkin / wellbeing**: decrease" in text
    assert "ignored 80%" in text
    assert "**content-recommend / philosophy longform**: increase" in text
    assert "bad" not in text


def test_engagement_post_accepts_content_mix_without_insights(tmp_path, monkeypatch):
    from tasks import engagement_analyze_post as post
    monkeypatch.setattr(post, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(
        post, "CONTENT_MIX_FILE",
        tmp_path / "memory" / "system" / "engagement_content_mix.md",
    )
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({
            "content_mix": [
                {"target": "checkin", "mode": "connection", "weight": "increase",
                 "rationale": "connection prompts got replies"}
            ]
        })),
    )

    assert post.main() == 0
    text = (tmp_path / "memory" / "system" / "engagement_content_mix.md").read_text(
        encoding="utf-8"
    )
    assert "**checkin / connection**: increase" in text
