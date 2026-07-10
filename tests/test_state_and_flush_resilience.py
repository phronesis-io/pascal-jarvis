"""Regression tests for the 7/10 audit survivors.

- heartbeat_state.json corruption must not wedge the scheduler forever:
  load_state archives the torn file and reseeds; save_state fsyncs the temp
  file before the rename so power loss can't publish a 0-byte state file.
- A night-queue digest send TIMEOUT must not count as delivered — one 15s
  local timeout used to unlink the entire queue (≤40 entries) and write a
  false FLUSH_DELIVERED audit row.
- Failed flush retries must be spaced by the breakpoint floor: the window
  arithmetic re-fired on every 10s tick during an outage, burning the
  5-retry expiry budget in ~1 minute (7/9: 13 entries expired).
"""

import json
import subprocess
import time

import core.heartbeat_loop as hbl
from core.heartbeat import HeartbeatRunner
from core.timeutil import now_local_str


def _runner(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    heartbeat_file = tmp_path / "HEARTBEAT.md"
    heartbeat_file.write_text("### t\n- interval: 1h\n- prompt: hi\n")
    return HeartbeatRunner(
        jarvis_dir=tmp_path,
        heartbeat_file=heartbeat_file,
        state_file=tmp_path / "heartbeat_state.json",
        memory_dir=memory_dir,
        model="opus",
        idle_judge=False,
    )


# ── load_state tolerates a torn/corrupt state file ───────────────────


def test_load_state_corrupt_file_archives_and_reseeds(tmp_path):
    runner = _runner(tmp_path)
    (tmp_path / "heartbeat_state.json").write_text('{"tasks": {"t"')  # torn write
    assert runner.load_state() == {}
    # evidence archived, original moved aside — the next cycle can run
    corrupt = tmp_path / "heartbeat_state.json.corrupt"
    assert corrupt.exists() and corrupt.read_text() == '{"tasks": {"t"'
    assert not (tmp_path / "heartbeat_state.json").exists()
    # scheduler recovers: a save/load roundtrip works again
    runner.save_state({"tasks": {}})
    assert runner.load_state() == {"tasks": {}}


def test_load_state_zero_byte_file_reseeds(tmp_path):
    # the exact APFS power-loss artifact: rename metadata landed, data didn't
    runner = _runner(tmp_path)
    (tmp_path / "heartbeat_state.json").write_text("")
    assert runner.load_state() == {}
    assert (tmp_path / "heartbeat_state.json.corrupt").exists()


def test_load_state_valid_file_untouched(tmp_path):
    runner = _runner(tmp_path)
    runner.save_state({"tasks": {"a": 1}})
    assert runner.load_state() == {"tasks": {"a": 1}}
    assert not (tmp_path / "heartbeat_state.json.corrupt").exists()


def test_save_state_fsyncs_before_rename(tmp_path, monkeypatch):
    """The data must be forced to disk BEFORE os.replace publishes the name —
    otherwise a forced shutdown can leave a torn file under the final name."""
    import core.heartbeat as hb
    events = []
    real_fsync, real_replace = hb.os.fsync, hb.os.replace
    monkeypatch.setattr(hb.os, "fsync",
                        lambda fd: (events.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(hb.os, "replace",
                        lambda a, b: (events.append("replace"),
                                      real_replace(a, b)))
    _runner(tmp_path).save_state({"x": 1})
    assert events == ["fsync", "replace"]


# ── digest send timeout ≠ delivered ──────────────────────────────────


def _plant_queue(tmp_path, texts):
    with open(tmp_path / hbl.NIGHT_QUEUE_FILE, "w") as f:
        for t in texts:
            f.write(json.dumps(
                {"ts": now_local_str("%Y-%m-%d %H:%M"),
                 "epoch": int(time.time()),
                 "text": t, "source": "content-recommend"},
                ensure_ascii=False) + "\n")


def test_digest_send_timeout_is_retryable_not_delivered(tmp_path, monkeypatch):
    _plant_queue(tmp_path, ["深夜消息"])

    def timeout_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 15))

    monkeypatch.setattr(hbl.subprocess, "run", timeout_run)
    monkeypatch.setattr(hbl.time, "sleep", lambda s: None)

    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_RETRYABLE
    # entries stay queued with a bumped retry count — NOT unlinked
    kept = [json.loads(l) for l in
            (tmp_path / hbl.NIGHT_QUEUE_FILE).read_text().splitlines()]
    assert len(kept) == 1 and kept[0]["retries"] == 1
    # the audit says retryable, never a false "delivered"
    rows = [json.loads(l) for l in
            (tmp_path / hbl.QUIET_FLUSH_AUDIT_FILE).read_text().splitlines()]
    assert [r["status"] for r in rows] == [hbl.FLUSH_RETRYABLE]
    # no outbox row → the dedup window can't suppress the eventual re-send
    assert not (tmp_path / "heartbeat_outbox.jsonl").exists()
    # attempt stamp written → _should_flush spaces the next retry
    assert (tmp_path / hbl.BATCH_ATTEMPT_STAMP).exists()


def test_single_message_timeout_still_assumes_delivered(monkeypatch):
    """The single-message duplicate-vs-loss tradeoff is unchanged: only the
    digest path opts out of assume-delivered."""
    def timeout_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr(hbl.subprocess, "run", timeout_run)
    assert hbl._lark_send_text("hello", "ou_test") is True
    assert hbl._lark_send_text(
        "hello", "ou_test", assume_delivered_on_timeout=False) is False


# ── failed flush retries are spaced, not tick-speed ──────────────────


def test_failed_flush_retries_are_spaced(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: False)
    (tmp_path / hbl.NIGHT_QUEUE_FILE).write_text('{"text":"x"}\n')
    now = time.time()
    hbl._stamp_flush(tmp_path, now=now - 4 * 3600)   # a window is long overdue
    assert hbl._should_flush(tmp_path, minutes_of_day=11 * 60, now=now)
    # a flush attempt just failed → the next ticks stay silent
    (tmp_path / hbl.BATCH_ATTEMPT_STAMP).write_text(str(now - 10))
    assert not hbl._should_flush(tmp_path, minutes_of_day=11 * 60, now=now)
    # …including on the user-activity breakpoint path
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: True)
    assert not hbl._should_flush(tmp_path, minutes_of_day=11 * 60, now=now)
    # floor passed → the retry is allowed again
    (tmp_path / hbl.BATCH_ATTEMPT_STAMP).write_text(
        str(now - hbl.BREAKPOINT_FLUSH_MIN_GAP_S - 1))
    assert hbl._should_flush(tmp_path, minutes_of_day=11 * 60, now=now)


def test_flush_failure_writes_attempt_stamp(tmp_path, monkeypatch):
    _plant_queue(tmp_path, ["消息"])
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: False)
    before = time.time()
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_RETRYABLE
    assert float((tmp_path / hbl.BATCH_ATTEMPT_STAMP).read_text()) >= before


def test_successful_flush_writes_no_attempt_stamp(tmp_path, monkeypatch):
    """The stamp only exists to space FAILED retries — a success must not
    delay the next window/breakpoint flush."""
    _plant_queue(tmp_path, ["消息"])
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: True)
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert not (tmp_path / hbl.BATCH_ATTEMPT_STAMP).exists()
