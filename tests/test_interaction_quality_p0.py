"""Regression tests for the 2026-06-11 interaction-quality P0 batch.

Covers docs/prd_interaction_quality.md REQ-02/03/04/05:
- run_cycle cross-process mutex (heartbeat_state.lock flock)
- error gate catching header-wrapped API failures
- outbox content dedup window
- interval override sidecar (W3.1 adaptive frequency)
"""

import fcntl
import json
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
    assert looks_like_error(text)


def test_wrapped_error_with_emoji_header_is_caught():
    text = "**🎯 Intent**\n\nFailed to authenticate. API Error: 403 Request not allowed"
    assert looks_like_error(text)


def test_legitimate_message_mentioning_api_late_is_not_flagged():
    text = ("今晚 Doctor Stretch 20:00 开始。课后我会给你一条闭环卡，"
            "记得记录今天拉到的位置和感受，回来按卡片提示回复就行。")
    assert not looks_like_error(text)


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
    # Repeated suggestion compounds from the current override
    _apply_adaptations([
        {"target": "free-time-nudge", "suggestion": "reduce frequency further"},
    ])
    overrides = json.loads((tmp_path / "interval_overrides.json").read_text())
    assert overrides == {"free-time-nudge": 14400}
