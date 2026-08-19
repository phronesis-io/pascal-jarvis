"""Host sleep accounting — how much of the recent past the machine was awake.

2026-08-19 audit. Between 08-17 21:12 and 08-19 13:02 the MacBook sat closed
on battery. Jarvis created 2 cards a day instead of 76, every intent for
08-18 expired or was skipped, and one heartbeat cycle took 12 hours because a
DarkWake window only advances it by seconds. Nothing was broken: the host was
not there.

Two instruments measured that absence and disagreed by two orders of
magnitude:

- ``daemon.py`` overshoots its own 1s-chunk sleep and got it right — 38
  detections, 39.4h total.
- ``core.heartbeat_loop`` overshot its own 10s nap only, so any hour the host
  slept *during a model call* was invisible. The same 39 hours were recorded
  as 0.7 in ``sched_events``.

macOS hands out an exact meter for free: ``time.time()`` counts system sleep,
``time.monotonic()`` (mach_absolute_time) does not, so the drift between them
IS the host's sleep, no matter where in the tick it happened. Measured on the
production host minutes after the incident: drift since boot 39.7h against
the daemon's independently observed 39.4h.

Where the platform's monotonic clock *does* include suspend (Linux
CLOCK_BOOTTIME semantics), the drift stays ~0 and every caller degrades to
"no host sleep detected" — the old behaviour, never a false positive.

The recorded episodes also make "age" honest. A component last heard from 39h
ago that was only powered for 20 minutes of that window is not stale; the
staleness checks that page on wall-clock age produce a wave of false red on
every lid-open. ``awake_age`` is the age those checks actually mean.

Single writer: the guardian daemon owns ``record``. Everyone else reads.
"""

from __future__ import annotations

import time
from pathlib import Path

# Same floor as daemon.SLEEP_GAP_THRESHOLD and heartbeat_loop's: below this a
# gap is scheduling jitter or a small clock correction, not the host leaving.
SLEEP_GAP_THRESHOLD_S = 120

SLEEP_LOG = "data/host_sleep.jsonl"
# ~500 episodes is months of ordinary lid-close nights, and one bad night of
# DarkWake churn (38 in this incident) cannot push real history out of reach.
SLEEP_LOG_KEEP = 500


def drift(now_wall: float | None = None, now_mono: float | None = None) -> float:
    """Seconds by which the wall clock leads the monotonic clock."""
    wall = time.time() if now_wall is None else float(now_wall)
    mono = time.monotonic() if now_mono is None else float(now_mono)
    return wall - mono


def gap_from(wall_elapsed_s: float, mono_elapsed_s: float,
             threshold_s: float = SLEEP_GAP_THRESHOLD_S) -> float:
    """Host sleep inside an interval, from its two elapsed measurements.

    The whole arithmetic of this module in one place, so the loop that
    brackets a tick and the meter that brackets a daemon cycle cannot drift
    apart. Sub-threshold differences are scheduling jitter or a small clock
    correction, not the host leaving.
    """
    gap = float(wall_elapsed_s) - float(mono_elapsed_s)
    return gap if gap >= threshold_s else 0.0


class Meter:
    """In-memory host-sleep meter for one long-running loop.

    ``gap()`` returns the host sleep observed since the previous call, so the
    caller may spend that interval in a 600s model call without the sleep
    hiding inside it.
    """

    def __init__(self, now_wall: float | None = None,
                 now_mono: float | None = None,
                 threshold_s: float = SLEEP_GAP_THRESHOLD_S) -> None:
        self.threshold_s = float(threshold_s)
        self._wall = time.time() if now_wall is None else float(now_wall)
        self._mono = time.monotonic() if now_mono is None else float(now_mono)

    def gap(self, now_wall: float | None = None,
            now_mono: float | None = None) -> float:
        wall = time.time() if now_wall is None else float(now_wall)
        mono = time.monotonic() if now_mono is None else float(now_mono)
        wall_delta = wall - self._wall
        mono_delta = mono - self._mono
        self._wall, self._mono = wall, mono
        if mono_delta < 0:
            # The monotonic clock restarted (a reboot, on platforms where it
            # counts from boot). The machine was away for the whole wall
            # interval, which is absence too — but the drift arithmetic would
            # add the pre-reboot uptime on top, so use the wall interval.
            return wall_delta if wall_delta >= self.threshold_s else 0.0
        return gap_from(wall_delta, mono_delta, self.threshold_s)


def _log_path(root: Path | str) -> Path:
    return Path(root) / SLEEP_LOG


def record(root: Path | str, gap_seconds: float,
           end_epoch: float | None = None) -> dict | None:
    """Append one host-sleep episode. Returns the stored row, or None."""
    gap = float(gap_seconds or 0)
    if gap < SLEEP_GAP_THRESHOLD_S:
        return None
    end = time.time() if end_epoch is None else float(end_epoch)
    row = {"start": round(end - gap, 3), "end": round(end, 3),
           "seconds": round(gap, 3)}
    from core.jsonl import append_jsonl

    append_jsonl(_log_path(root), row, keep_last=SLEEP_LOG_KEEP)
    return row


def episodes(root: Path | str) -> list[dict]:
    from core.jsonl import read_jsonl

    return read_jsonl(_log_path(root))


def slept_between(root: Path | str, start_epoch: float,
                  end_epoch: float) -> float:
    """Recorded host sleep overlapping ``[start_epoch, end_epoch]``."""
    start, end = float(start_epoch), float(end_epoch)
    if end <= start:
        return 0.0
    total = 0.0
    for row in episodes(root):
        try:
            row_start = float(row.get("start", 0))
            row_end = float(row.get("end", 0))
        except (TypeError, ValueError):
            continue
        overlap = min(end, row_end) - max(start, row_start)
        if overlap > 0:
            total += overlap
    return min(total, end - start)


def awake_age(root: Path | str, since_epoch: float,
              now: float | None = None) -> float:
    """Age of ``since_epoch`` counting only time the host was actually up.

    With no recorded episodes this is the plain wall age, so a fresh install
    (or a host that never sleeps) behaves exactly as before.
    """
    moment = time.time() if now is None else float(now)
    wall = moment - float(since_epoch)
    if wall <= 0:
        return wall
    return max(0.0, wall - slept_between(root, since_epoch, moment))
