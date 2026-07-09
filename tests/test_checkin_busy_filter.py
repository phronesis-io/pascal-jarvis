"""Tests for checkin_busy_filter — the calendar BUSY gate of checkin_pre.sh.

Regression for the 6/25–7/8 outage: a 17-day freebusy block (Iceland trip)
satisfied start<=now<end on every attempt and silently muted all check-ins.
All timestamps are explicit-offset ISO strings and `now` is passed in aware,
so nothing here depends on the machine's local timezone.
"""
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tasks"))

import checkin_busy_filter as f  # noqa: E402

# Fixed "now": 2026-07-08 12:00 UTC (= 20:00 Beijing)
NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def block(start, end):
    return {"start_time": start, "end_time": end, "rsvp_status": "accept"}


# The real freebusy block that caused the outage.
TRIP = block("2026-06-24T20:00:00+08:00", "2026-07-11T20:30:00+08:00")


def test_multi_day_event_does_not_mute():
    out = f.transition_context([TRIP], now=NOW)
    assert out != "BUSY"
    assert "multi_day_event" in out
    assert "no_upcoming_events" in out


def test_normal_meeting_still_blocks():
    meeting = block("2026-07-08T19:30:00+08:00", "2026-07-08T20:30:00+08:00")
    assert f.transition_context([meeting], now=NOW) == "BUSY"


def test_boundary_20h_ignored_just_under_blocks():
    # Exactly 20h → trip-like, ignored; 19h59m → still treated as a meeting.
    long_ev = block("2026-07-08T00:00:00+08:00", "2026-07-08T20:00:00+08:00")
    assert f.transition_context([long_ev], now=NOW) != "BUSY"
    short_ev = block("2026-07-08T00:02:00+08:00", "2026-07-08T20:01:00+08:00")
    assert f.transition_context([short_ev], now=NOW) == "BUSY"


def test_meeting_just_ended_is_transition():
    ended = block("2026-07-08T19:00:00+08:00", "2026-07-08T19:55:00+08:00")
    out = f.transition_context([ended], now=NOW)
    assert "transition: meeting ended 5m ago" in out
    assert "best_moment: post-meeting transition" in out


def test_tight_window_before_next_meeting_blocks():
    upcoming = block("2026-07-08T20:10:00+08:00", "2026-07-08T21:00:00+08:00")
    assert f.transition_context([upcoming], now=NOW) == "BUSY"


def test_future_long_event_does_not_trigger_tight_window():
    # Landmine 2: a multi-day event starting in 10 min must not count as
    # next_event, else the <20m tight-window branch re-mutes check-ins.
    future_trip = block("2026-07-08T20:10:00+08:00", "2026-07-12T20:00:00+08:00")
    out = f.transition_context([future_trip], now=NOW)
    assert out != "BUSY"
    assert "no_upcoming_events" in out


def test_large_free_block_signal():
    upcoming = block("2026-07-08T22:00:00+08:00", "2026-07-08T23:00:00+08:00")
    out = f.transition_context([upcoming], now=NOW)
    assert "next_event_in: 120m" in out
    assert "large_free_block: 120m available" in out


def test_trip_plus_real_meeting_meeting_still_wins():
    meeting = block("2026-07-08T19:30:00+08:00", "2026-07-08T20:30:00+08:00")
    assert f.transition_context([TRIP, meeting], now=NOW) == "BUSY"


def test_empty_calendar():
    out = f.transition_context([], now=NOW)
    assert out == "no_upcoming_events: rest of day is clear"


def test_main_prints_context(monkeypatch, capsys):
    payload = json.dumps({"ok": True, "data": [TRIP]})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    f.main()
    out = capsys.readouterr().out
    assert "multi_day_event" in out
    assert "BUSY" not in out


def test_main_bad_json_reports_calendar_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    f.main()
    assert capsys.readouterr().out.startswith("calendar_error:")
