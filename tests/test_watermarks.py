"""Tests for core/watermarks.py (REQ-12 channel watermark monitoring)."""

import json
import time

from core.watermarks import channel_watermark_report


def _setup(tmp_path, hb_content, state=None, overrides=None,
           delivery=None, night_queue_lines=None):
    (tmp_path / "HEARTBEAT.md").write_text(hb_content)
    if state is not None:
        (tmp_path / "heartbeat_state.json").write_text(json.dumps(state))
    if overrides is not None:
        (tmp_path / "interval_overrides.json").write_text(json.dumps(overrides))
    if delivery is not None:
        (tmp_path / ".delivery_state.json").write_text(json.dumps(delivery))
    if night_queue_lines:
        (tmp_path / "night_queue.jsonl").write_text("\n".join(night_queue_lines) + "\n")


HB = "### feed\n- interval: 1h\n- prompt: p\n\n### checkin\n- interval: 2h\n- prompt: q\n"


def test_healthy_channels(tmp_path):
    now = time.time()
    _setup(tmp_path, HB, state={
        "feed": {"last_run": int(now - 1800)},
        "checkin": {"last_run": int(now - 3600)},
    })
    report = channel_watermark_report(tmp_path, now=now)
    assert "✓ All task channels within expected cadence" in report
    assert "STARVED" not in report


def test_starved_channel_flagged(tmp_path):
    now = time.time()
    # feed last ran 5h ago against a 1h interval — well past the 2x factor
    _setup(tmp_path, HB, state={
        "feed": {"last_run": int(now - 5 * 3600)},
        "checkin": {"last_run": int(now - 3600)},
    })
    report = channel_watermark_report(tmp_path, now=now)
    assert "feed" in report and "STARVED" in report
    assert "checkin: " not in report  # healthy channel not listed


def test_short_cycle_task_gets_execution_jitter_grace(tmp_path):
    """A 1m task at 2m30s can be completing inside the current model batch.

    This is the production 2026-08-14 false page: intention-check succeeded,
    but its prior success receipt used the previous cycle's acquire time.
    """
    now = time.time()
    hb = "### intention-check\n- interval: 1m\n- prompt: p\n"
    _setup(tmp_path, hb, state={
        "intention-check": {
            "last_run": int(now - 30),
            "last_success": int(now - 150),
            "last_status": "ok",
        },
    })
    report = channel_watermark_report(tmp_path, now=now)
    assert "STARVED" not in report


def test_short_cycle_task_is_starved_after_jitter_grace(tmp_path):
    now = time.time()
    hb = "### intention-check\n- interval: 1m\n- prompt: p\n"
    _setup(tmp_path, hb, state={
        "intention-check": {
            "last_run": int(now - 30),
            "last_success": int(now - 181),
            "last_status": "ok",
        },
    })
    report = channel_watermark_report(tmp_path, now=now)
    assert "intention-check" in report and "STARVED" in report


def _age_install_stamp(tmp_path, age_s):
    """Pre-create the install stamp `age_s` seconds in the past."""
    import os
    stamp = tmp_path / "data" / ".install_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    old = time.time() - age_s
    os.utime(stamp, (old, old))


def test_never_ran_flagged(tmp_path):
    # Install old enough that the fresh-install grace (2x interval) is over
    _age_install_stamp(tmp_path, 3 * 3600)  # feed interval 1h → grace 2h
    _setup(tmp_path, HB, state={"checkin": {"last_run": int(time.time())}})
    report = channel_watermark_report(tmp_path)
    assert "feed" in report and "NEVER run" in report


def test_never_ran_suppressed_during_fresh_install_grace(tmp_path):
    # 2026-07-13: a collaborator's FIRST self-diagnostic listed six
    # "has NEVER run" ⚠️ lines minutes after install — including
    # self-diagnostic reporting itself. Within 2x interval of the install
    # stamp a missing first run is informational, never ⚠️.
    _age_install_stamp(tmp_path, 600)  # installed 10min ago
    _setup(tmp_path, HB, state={"checkin": {"last_run": int(time.time())}})
    report = channel_watermark_report(tmp_path)
    assert "NEVER run" not in report
    assert "first run pending" in report and "feed" in report


def test_missing_install_stamp_self_heals_as_fresh(tmp_path):
    # No stamp (pre-existing install upgrading to this code): the first
    # report stamps `now`, so never-run suppression applies — harmless,
    # because live installs already carry last_success for real tasks.
    _setup(tmp_path, HB, state={"checkin": {"last_run": int(time.time())}})
    report = channel_watermark_report(tmp_path)
    assert "NEVER run" not in report
    assert (tmp_path / "data" / ".install_stamp").exists()


def test_override_interval_respected(tmp_path):
    now = time.time()
    # 3h since last run: starved under the 1h default (2x = 2h),
    # healthy under a 4h override
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now - 3 * 3600)},
                  "checkin": {"last_run": int(now)}},
           overrides={"feed": 4 * 3600})
    report = channel_watermark_report(tmp_path, now=now)
    assert "STARVED" not in report


def test_open_circuit_flagged(tmp_path):
    now = time.time()
    _setup(tmp_path, HB, state={
        "feed": {"last_run": int(now - 600),
                 "circuit": {"disabled_until": now + 1800}},
        "checkin": {"last_run": int(now)},
    })
    report = channel_watermark_report(tmp_path, now=now)
    assert "circuit OPEN" in report


def _epoch_at(hour, minute=0):
    """Epoch seconds for today at hour:minute, system-local tz."""
    from datetime import datetime
    return datetime.now().replace(hour=hour, minute=minute,
                                  second=0, microsecond=0).timestamp()


def test_delivery_failures_and_night_queue_surface(tmp_path):
    now = time.time()
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)}, "checkin": {"last_run": int(now)}},
           delivery={"consec_fails": 4},
           night_queue_lines=['{"text":"a"}', '{"text":"b"}'])
    report = channel_watermark_report(tmp_path, now=now)
    assert "4 consecutive send failures" in report
    assert "Batch queue: 2" in report


def test_unified_delivery_future_window_is_normal(tmp_path):
    now = time.time()
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)},
                  "checkin": {"last_run": int(now)}},
           delivery={
               "queued": 4,
               "consec_fails": 0,
               "queued_items": [{
                   "created_epoch": now - 300,
                   "next_attempt_epoch": now + 12 * 3600,
                   "attempts": 0,
                   "last_error": "global_daily_cap",
               }] * 4,
           })
    report = channel_watermark_report(tmp_path, now=now)
    assert "Unified delivery: 4 item(s) deferred" in report
    assert "⚠️ Unified delivery" not in report


def test_unified_delivery_current_flush_window_is_normal(tmp_path):
    now = time.time()
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)},
                  "checkin": {"last_run": int(now)}},
           delivery={
               "queued": 1,
               "consec_fails": 0,
               "queued_items": [{
                   "created_epoch": now - 60,
                   "next_attempt_epoch": None,
                   "attempts": 0,
                   "last_error": "",
               }],
           })
    report = channel_watermark_report(tmp_path, now=now)
    assert "automatic flush window" in report
    assert "⚠️ Unified delivery" not in report


def test_unified_delivery_overdue_after_flush_retry_is_warning(tmp_path):
    now = time.time()
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)},
                  "checkin": {"last_run": int(now)}},
           delivery={
               "queued": 2,
               "consec_fails": 0,
               "queued_items": [{
                   "created_epoch": now - 3600,
                   "next_attempt_epoch": now - 1800,
                   "attempts": 1,
                   "last_error": "transport_retry",
               }],
           })
    report = channel_watermark_report(tmp_path, now=now)
    assert "⚠️ Unified delivery: 1 of 2" in report
    assert "overdue after automatic flush/retry" in report


def test_queue_during_quiet_hours_is_normal(tmp_path):
    now = _epoch_at(2, 0)  # 02:00 — quiet hours
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)}, "checkin": {"last_run": int(now)}},
           night_queue_lines=['{"text":"a"}'])
    report = channel_watermark_report(tmp_path, now=now)
    assert "held for the morning digest" in report
    assert "STUCK" not in report and "⚠️ Batch queue" not in report


def test_queue_awaiting_next_window_is_normal(tmp_path):
    # 12:49 daytime queue entry, last flush 12:33 (after the 10:00 window) —
    # exactly the 2026-06-12 false alarm: awaiting the 13:30 window, not stuck
    now = _epoch_at(13, 8)
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)}, "checkin": {"last_run": int(now)}},
           night_queue_lines=['{"text":"a"}'])
    (tmp_path / ".batch_last_flush").write_text(str(_epoch_at(12, 33)))
    report = channel_watermark_report(tmp_path, now=now)
    assert "awaiting next batch window (13:30)" in report
    assert "STUCK" not in report


def test_queue_stuck_past_window_flagged(tmp_path):
    # Last flush 09:00, window opened 13:30, it's now 14:00 — flush missed
    now = _epoch_at(14, 0)
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)}, "checkin": {"last_run": int(now)}},
           night_queue_lines=['{"text":"a"}'])
    (tmp_path / ".batch_last_flush").write_text(str(_epoch_at(9, 0)))
    report = channel_watermark_report(tmp_path, now=now)
    assert "STUCK" in report and "13:30 window" in report


def test_queue_after_last_window_points_to_tomorrow(tmp_path):
    # 18:00 queue entry at 19:00, flushed at the 17:30 window already —
    # next chance is tomorrow's first window
    now = _epoch_at(19, 0)
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)}, "checkin": {"last_run": int(now)}},
           night_queue_lines=['{"text":"a"}'])
    (tmp_path / ".batch_last_flush").write_text(str(_epoch_at(18, 30)))
    report = channel_watermark_report(tmp_path, now=now)
    assert "tomorrow 10:00" in report
    assert "STUCK" not in report


def test_missing_files_no_crash(tmp_path):
    (tmp_path / "HEARTBEAT.md").write_text(HB)
    report = channel_watermark_report(tmp_path)
    assert "Channel Watermarks" in report  # never-ran flags, but no exception
