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


def test_never_ran_flagged(tmp_path):
    _setup(tmp_path, HB, state={"checkin": {"last_run": int(time.time())}})
    report = channel_watermark_report(tmp_path)
    assert "feed" in report and "NEVER run" in report


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


def test_delivery_failures_and_night_queue_surface(tmp_path):
    now = time.time()
    _setup(tmp_path, HB,
           state={"feed": {"last_run": int(now)}, "checkin": {"last_run": int(now)}},
           delivery={"consec_fails": 4},
           night_queue_lines=['{"text":"a"}', '{"text":"b"}'])
    report = channel_watermark_report(tmp_path, now=now)
    assert "4 consecutive send failures" in report
    assert "Night queue: 2" in report


def test_missing_files_no_crash(tmp_path):
    (tmp_path / "HEARTBEAT.md").write_text(HB)
    report = channel_watermark_report(tmp_path)
    assert "Channel Watermarks" in report  # never-ran flags, but no exception
