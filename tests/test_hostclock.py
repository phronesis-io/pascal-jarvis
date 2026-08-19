"""Host sleep meter and awake-age accounting (2026-08-19 audit)."""

from core import hostclock


def test_meter_ignores_ordinary_tick():
    meter = hostclock.Meter(now_wall=1000.0, now_mono=500.0)
    # 30s of real work: both clocks advance together.
    assert meter.gap(now_wall=1030.0, now_mono=530.0) == 0.0


def test_meter_ignores_long_model_call():
    """The regression that made heartbeat_loop's own meter useless.

    A 600s Claude call must not be mislabeled as host sleep — and the previous
    nap-only measurement bought that safety by going blind to any sleep that
    happened during the call.
    """
    meter = hostclock.Meter(now_wall=1000.0, now_mono=500.0)
    assert meter.gap(now_wall=1600.0, now_mono=1100.0) == 0.0


def test_meter_sees_sleep_inside_a_long_call():
    # 39h of wall time, 10 minutes of it awake: the 08-17→08-19 shape.
    meter = hostclock.Meter(now_wall=1000.0, now_mono=500.0)
    gap = meter.gap(now_wall=1000.0 + 39 * 3600, now_mono=500.0 + 600)
    assert round(gap) == 39 * 3600 - 600


def test_meter_below_threshold_is_jitter():
    meter = hostclock.Meter(now_wall=1000.0, now_mono=500.0,
                            threshold_s=120)
    assert meter.gap(now_wall=1119.0, now_mono=500.0) == 0.0
    meter = hostclock.Meter(now_wall=1000.0, now_mono=500.0,
                            threshold_s=120)
    assert meter.gap(now_wall=1121.0, now_mono=500.0) == 121.0


def test_meter_handles_monotonic_restart():
    """A reboot resets a boot-based monotonic clock; drift math would add the
    whole previous uptime on top of the real absence."""
    meter = hostclock.Meter(now_wall=1000.0, now_mono=500_000.0)
    assert meter.gap(now_wall=1000.0 + 900, now_mono=3.0) == 900.0


def test_meter_baseline_advances_each_call():
    meter = hostclock.Meter(now_wall=0.0, now_mono=0.0)
    assert meter.gap(now_wall=3600.0, now_mono=10.0) == 3590.0
    # The same sleep is not counted twice on the next tick.
    assert meter.gap(now_wall=3610.0, now_mono=20.0) == 0.0


def test_record_and_slept_between(tmp_path):
    assert hostclock.record(tmp_path, 30) is None  # below threshold
    row = hostclock.record(tmp_path, 3600, end_epoch=10_000.0)
    assert row == {"start": 6400.0, "end": 10_000.0, "seconds": 3600.0}
    assert hostclock.slept_between(tmp_path, 0, 20_000) == 3600
    # Partial overlap counts only the overlapping part.
    assert hostclock.slept_between(tmp_path, 8200.0, 10_000.0) == 1800.0
    assert hostclock.slept_between(tmp_path, 10_000.0, 20_000.0) == 0.0


def test_awake_age_discounts_recorded_sleep(tmp_path):
    hostclock.record(tmp_path, 39 * 3600, end_epoch=100_000.0)
    # Something last seen right before the sleep, checked 10 min after wake.
    age = hostclock.awake_age(tmp_path, 100_000.0 - 39 * 3600 - 60,
                             now=100_600.0)
    assert round(age) == 660


def test_awake_age_without_episodes_is_wall_age(tmp_path):
    assert hostclock.awake_age(tmp_path, 900.0, now=1500.0) == 600.0


def test_slept_between_never_exceeds_window(tmp_path):
    hostclock.record(tmp_path, 3600, end_epoch=10_000.0)
    hostclock.record(tmp_path, 3600, end_epoch=10_000.0)  # duplicate rows
    assert hostclock.slept_between(tmp_path, 9_000.0, 10_000.0) == 1000.0
