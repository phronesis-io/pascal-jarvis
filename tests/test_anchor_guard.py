"""Tests for core.anchor_guard — grounding clock-time anchors in narration.

Anchored on the 2026-06-22 incident: a cross-session line claimed "今早 6:35 …敲 ls
…撞月度消费上限" with no log line at 6:35. The guard must catch that and pass the
true line ("14:31 开始撞额度") that the same logs do support.
"""
from datetime import datetime
from pathlib import Path

from core.anchor_guard import (
    extract_anchors,
    is_groundable,
    unverified_anchors,
)

NOW = datetime(2026, 6, 22, 14, 50, 0)


def _write_log(tmp_path: Path) -> list[Path]:
    """A log tail that has activity around 14:31 today but nothing near 6:35."""
    jl = tmp_path / "jarvis.log"
    jl.write_text(
        "\n".join(
            [
                '{"ts": "2026-06-22T14:31:02", "msg": "spend limit"}',
                '{"ts": "2026-06-22T14:33:24", "msg": "fallback"}',
                "[2026-06-22 14:44:46] [INFO] Received: ...",
                '{"ts": "2026-06-21T18:10:00", "msg": "yesterday evening"}',
            ]
        ),
        encoding="utf-8",
    )
    return [jl, tmp_path / "daemon.log"]  # daemon.log absent → must not raise


def test_extract_basic_hhmm():
    anchors = extract_anchors("今早 6:35 撞到上限", now=NOW)
    assert [a.raw for a in anchors] == ["6:35"]
    assert anchors[0].minute_of_day == 6 * 60 + 35
    assert anchors[0].date == "2026-06-22"


def test_no_anchor_when_no_clock_time():
    assert extract_anchors("今天下午额度在撞，没具体时间", now=NOW) == []
    assert is_groundable("今天下午额度在撞", now=NOW)


def test_fabricated_anchor_is_unverified(tmp_path):
    logs = _write_log(tmp_path)
    bad = unverified_anchors("今早 6:35 你在 repos 敲 ls 撞到上限", logs, now=NOW)
    assert [a.raw for a in bad] == ["6:35"]
    assert not is_groundable("今早 6:35 撞到上限", log_paths=logs, now=NOW)


def test_real_anchor_is_grounded(tmp_path):
    logs = _write_log(tmp_path)
    # 14:31 exists in the log → grounded → nothing unverified.
    assert unverified_anchors("14:31 开始撞额度", logs, now=NOW) == []
    assert is_groundable("14:31 开始撞额度", log_paths=logs, now=NOW)


def test_tolerance_window(tmp_path):
    logs = _write_log(tmp_path)
    # 14:45 is within ±20min of the 14:31/14:44 log lines → grounded.
    assert unverified_anchors("14:45 又撞了一次", logs, now=NOW) == []
    # 13:00 is >20min from any log line → unverified.
    assert [a.raw for a in unverified_anchors("13:00 撞的", logs, now=NOW)] == ["13:00"]


def test_yesterday_resolves_to_prior_date(tmp_path):
    logs = _write_log(tmp_path)
    # 昨晚 → yesterday; "下午"/"晚上" bumps 6 → 18; log has 2026-06-21T18:10.
    assert unverified_anchors("昨晚 6:10 跑了任务", logs, now=NOW) == []


def test_pm_qualifier_bumps_hour(tmp_path):
    logs = _write_log(tmp_path)
    # "下午 2:31" → 14:31 today → grounded.
    assert unverified_anchors("下午 2:31 撞额度", logs, now=NOW) == []


def test_fails_open_when_no_logs(tmp_path):
    # Empty/unreadable logs → cannot prove anything → never block real nudges.
    missing = [tmp_path / "jarvis.log", tmp_path / "daemon.log"]
    assert unverified_anchors("今早 6:35 撞到上限", missing, now=NOW) == []


def test_does_not_match_inside_digit_runs():
    # version-ish / id-ish strings must not be read as clock times.
    assert extract_anchors("跑了 123:456 次，端口 8080", now=NOW) == []
