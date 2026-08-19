"""Absence receipts — the 2026-08-19 "39h asleep, nobody told" incident."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from core import absence

TZ = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _fixed_timezone(monkeypatch):
    """Pin local time so quiet-hour arithmetic is not CI-machine dependent."""
    monkeypatch.setattr(absence, "_local",
                        lambda epoch: datetime.fromtimestamp(float(epoch), TZ))


def at(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 8, day, hour, minute, tzinfo=TZ).timestamp()


def test_overnight_lid_close_stays_silent(tmp_path):
    """The card must not become a daily 'good morning, I slept' notification."""
    start, end = at(18, 23, 40), at(19, 8, 20)
    assert absence.observe(tmp_path, end - start, now=end) is None
    assert absence.observe(
        tmp_path, 0, now=end + absence.AWAKE_CONFIRM_SECONDS + 1) is None


def test_working_day_absence_reports_once(tmp_path):
    start, end = at(17, 21, 12), at(19, 13, 2)
    assert absence.observe(tmp_path, end - start, now=end) is None
    report = absence.observe(
        tmp_path, 0, now=end + absence.AWAKE_CONFIRM_SECONDS + 1)
    assert report is not None
    assert round(report.slept_seconds / 3600) == 40
    assert report.active_seconds >= absence.REPORT_ACTIVE_SECONDS
    # Reported exactly once: the episode is closed, not re-announced forever.
    assert absence.observe(tmp_path, 0, now=end + 4000) is None


def test_darkwake_bursts_stay_one_episode(tmp_path):
    """38 DarkWake-punctuated gaps were one absence, not 38 cards."""
    moment = at(18, 2, 0)
    for _ in range(12):
        assert absence.observe(tmp_path, 3600, now=moment) is None
        moment += 3600 + 20  # 20s of DarkWake between hour-long sleeps
    report = absence.observe(
        tmp_path, 0, now=moment + absence.AWAKE_CONFIRM_SECONDS + 1)
    assert report is not None
    assert report.gaps == 12
    assert round(report.slept_seconds / 3600) == 12


def test_a_confirmed_wake_between_gaps_splits_episodes(tmp_path):
    first_end = at(18, 3, 0)
    absence.observe(tmp_path, 3600, now=first_end)
    # Two hours genuinely awake, then a second sleep: a new episode.
    second_end = first_end + 2 * 3600 + 1800
    absence.observe(tmp_path, 1800, now=second_end)
    report = absence.observe(
        tmp_path, 0, now=second_end + absence.AWAKE_CONFIRM_SECONDS + 1)
    # Night-only, so nothing is reported, but the episode must not have
    # accumulated the earlier sleep either.
    assert report is None


def test_no_report_before_the_wake_is_confirmed(tmp_path):
    """Reporting inside a DarkWake would understate the absence and queue the
    card behind the next sleep."""
    start, end = at(17, 21, 12), at(19, 13, 2)
    absence.observe(tmp_path, end - start, now=end)
    assert absence.observe(tmp_path, 0, now=end + 60) is None
    assert absence.observe(
        tmp_path, 0, now=end + absence.AWAKE_CONFIRM_SECONDS + 1) is not None


def test_short_daytime_absence_is_not_worth_a_card(tmp_path):
    start, end = at(18, 13, 0), at(18, 15, 0)  # a two-hour commute
    absence.observe(tmp_path, end - start, now=end)
    assert absence.observe(
        tmp_path, 0, now=end + absence.AWAKE_CONFIRM_SECONDS + 1) is None


def test_active_seconds_counts_only_non_quiet_hours():
    night = absence.active_seconds(at(18, 0, 0), at(18, 6, 0))
    assert night == 0
    day = absence.active_seconds(at(18, 10, 0), at(18, 16, 0))
    assert round(day / 3600) == 6


def test_card_names_what_the_absence_cost(tmp_path):
    (tmp_path / "sched_events.jsonl").write_text(
        '{"ts": "2026-08-18 21:19:33", "event": "intent_expired", '
        '"name": "Prep: 项目评审"}\n'
        '{"ts": "2026-08-18 21:19:33", "event": "intent_expired", '
        '"name": "出门带东西清单 2026-08-18"}\n'
        '{"ts": "2026-08-18 21:19:33", "event": "intent_occurrence_skipped", '
        '"name": "每日日报"}\n',
        encoding="utf-8")
    report = absence.Report(start=at(17, 21, 12), end=at(19, 13, 2),
                            slept_seconds=39.4 * 3600,
                            active_seconds=16 * 3600, gaps=38)
    title, body = absence.build_card(tmp_path, report)

    assert title == "我离线了 1 天 15 小时"
    assert "Prep: 项目评审" in body
    assert "2 件事过期" in body and "1 次例行没跑" in body
    # Style contract: three lines, and an explicit "nothing for you to do".
    assert len(body.splitlines()) == 3
    assert "知道就行" in body
    # Honest about the cause instead of implying a fault to chase.
    assert "不是故障" in body


def test_card_without_misses_says_so(tmp_path):
    report = absence.Report(start=at(18, 10, 0), end=at(18, 20, 0),
                            slept_seconds=10 * 3600,
                            active_seconds=10 * 3600, gaps=1)
    _title, body = absence.build_card(tmp_path, report)
    assert "没有到期的事" in body
    assert len(body.splitlines()) == 3


def test_emit_creates_one_notice_card(tmp_path, monkeypatch):
    from core import memorial

    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return "mem_1", True

    monkeypatch.setattr(memorial, "create", fake_create)
    report = absence.Report(start=at(17, 21, 12), end=at(19, 13, 2),
                            slept_seconds=39 * 3600,
                            active_seconds=16 * 3600, gaps=38)
    assert absence.emit(tmp_path, report) is True
    (kwargs,) = calls
    assert kwargs["source"] == "host-absence"
    # A receipt, never a page: no alert class, and a stable identity so a
    # daemon restart mid-report cannot double-send the same episode.
    assert kwargs["attention"] == "notice"
    assert kwargs["dedup_key"] == f"absence:{int(report.end)}"


def test_reported_sleep_never_exceeds_the_window(tmp_path):
    """Two meters feed one episode; a clock correction must not produce a
    card claiming more absence than the window it names."""
    end = at(19, 13, 2)
    absence.observe(tmp_path, 3600, now=end)
    state = json.loads((tmp_path / absence.STATE_FILE).read_text())
    state["slept"] = 10 * 3600  # implausible overlap
    state["start"] = end - 8 * 3600
    (tmp_path / absence.STATE_FILE).write_text(json.dumps(state))
    report = absence.observe(
        tmp_path, 0, now=end + absence.AWAKE_CONFIRM_SECONDS + 1)
    assert report is not None
    assert report.slept_seconds == 8 * 3600
