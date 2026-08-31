"""A probe that runs but cannot read one of its sources must say so.

2026-08-25 → 08-31 the PGC pulse probe reported broken_first_party=None for
six days after the viewer moved off 127.0.0.1: the metric quietly vanished
from every snapshot and nothing flipped. The command contract now carries
``errors`` and the adapter turns each entry into a「失明 N 天」anomaly plus a
recovery when it clears; the digest pre-hook turns those into cards.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sources import metrics_probe

REPO = Path(__file__).resolve().parent.parent
PRE = REPO / "tasks" / "metrics_digest_pre.sh"
TZ = timezone(timedelta(hours=8))


def _cfg(tmp_path):
    return {"name": "pulse_demo", "command": "true", "timeout": 5,
            "snapshot_hour": 23, "history_file": str(tmp_path / "pulse_demo.jsonl"),
            "rules": [{"metric": "broken_first_party", "op": ">=", "value": 1}]}


def _collect(monkeypatch, tmp_path, payload, state, now):
    monkeypatch.setattr(metrics_probe, "_run_command", lambda cmd, timeout: (payload, None))
    monkeypatch.setattr(metrics_probe, "_now", lambda: now)
    return metrics_probe.collect(_cfg(tmp_path), state)


def _records(tmp_path):
    path = tmp_path / "pulse_demo.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_dead_endpoint_becomes_a_visible_blind_flip(monkeypatch, tmp_path):
    day1 = datetime(2026, 8, 25, 9, 0, tzinfo=TZ)
    dead = {"metrics": {"win_rate": 12.0},          # broken_first_party silently absent…
            "details": "report: 2026-08-25.json",
            "errors": {"metrics": "127.0.0.1: URLError: Connection refused"}}  # …but said out loud
    signals, state = _collect(monkeypatch, tmp_path, dead, {}, day1)
    blind = [s for s in signals if s["payload"]["metric"] == "blind:metrics"]
    assert len(blind) == 1
    assert blind[0]["event_id"] == "anomaly:blind:metrics:2026-08-25"
    assert "失明" in blind[0]["title"] and "第 1 天" in blind[0]["title"]
    assert "Connection refused" in blind[0]["title"]
    rec = [r for r in _records(tmp_path) if r["kind"] == "anomaly"][0]
    assert rec["metric"] == "blind:metrics" and rec["actual"] == 1
    assert rec["component"] == "metrics" and rec["error"].startswith("127.0.0.1")
    assert rec["since"] == day1.isoformat(timespec="seconds")
    assert state["blind_since"] == {"metrics": rec["since"]}

    # same day, still dead: edge-triggered — no second signal
    signals, state = _collect(monkeypatch, tmp_path, dead, state, day1 + timedelta(hours=2))
    assert not [s for s in signals if s["payload"]["metric"] == "blind:metrics"]

    # three days later, still dead: the count escalates and re-alerts
    signals, state = _collect(monkeypatch, tmp_path, dead, state, day1 + timedelta(days=3))
    blind = [s for s in signals if s["payload"]["metric"] == "blind:metrics"]
    assert len(blind) == 1 and blind[0]["payload"]["actual"] == 4
    assert "第 4 天" in blind[0]["title"]


def test_sight_restored_emits_recovery_once_and_rearms(monkeypatch, tmp_path):
    day1 = datetime(2026, 8, 25, 9, 0, tzinfo=TZ)
    dead = {"metrics": {}, "errors": {"metrics": "boom"}}
    alive = {"metrics": {"broken_first_party": 0}}
    _, state = _collect(monkeypatch, tmp_path, dead, {}, day1)
    signals, state = _collect(monkeypatch, tmp_path, alive, state, day1 + timedelta(days=2))
    rec = [s for s in signals if s["payload"]["kind"] == "recovery"]
    assert len(rec) == 1 and rec[0]["payload"]["metric"] == "blind:metrics"
    assert rec[0]["event_id"] == "recovery:blind:metrics:2026-08-27"
    assert "又读到了" in rec[0]["title"]
    assert state["blind_since"] == {}
    hist = _records(tmp_path)
    assert [r["kind"] for r in hist] == ["anomaly", "recovery"]
    assert hist[1]["component"] == "metrics" and hist[1]["tripped_on"] == "2026-08-25"
    # still alive: nothing more
    signals, state = _collect(monkeypatch, tmp_path, alive, state, day1 + timedelta(days=2, hours=2))
    assert signals == []
    # relapse later the same day alerts again (marker was cleared)
    signals, state = _collect(monkeypatch, tmp_path, dead, state, day1 + timedelta(days=2, hours=4))
    assert [s["payload"]["metric"] for s in signals] == ["blind:metrics"]


def test_malformed_errors_are_ignored(monkeypatch, tmp_path):
    now = datetime(2026, 8, 25, 9, 0, tzinfo=TZ)
    for payload in ({"metrics": {}, "errors": "nope"},
                    {"metrics": {}, "errors": {"": "x", "m": "", "n": None}},
                    {"metrics": {}, "errors": ["x"]}):
        signals, state = _collect(monkeypatch, tmp_path, payload, {}, now)
        assert signals == [] and state["blind_since"] == {}


def test_digest_pre_emits_the_blind_flip_as_a_card_record(monkeypatch, tmp_path):
    """End to end through the real adapter history → metrics-digest pre-hook."""
    import os
    now = datetime.now().astimezone()
    cfg = _cfg(tmp_path)
    mdir = tmp_path / "data" / "metrics"
    mdir.mkdir(parents=True)
    cfg["history_file"] = str(mdir / "pulse_demo.jsonl")
    monkeypatch.setattr(metrics_probe, "_run_command",
                        lambda c, t: ({"metrics": {}, "errors": {"metrics": "URLError: refused"}}, None))
    monkeypatch.setattr(metrics_probe, "_now", lambda: now)
    metrics_probe.collect(cfg, {})
    r = subprocess.run(["bash", str(PRE)], env={**os.environ, "JARVIS_DIR": str(tmp_path)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    recs = [json.loads(l) for l in r.stdout.splitlines()[1:]]
    assert len(recs) == 1
    assert recs[0]["kind"] == "anomaly" and recs[0]["metric"] == "blind:metrics"
    assert recs[0]["error"] == "URLError: refused" and recs[0]["actual"] == 1
