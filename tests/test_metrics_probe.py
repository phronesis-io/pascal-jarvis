"""Tests for the metrics_probe adapter (sources/README.md contract)."""

import json
import time
from datetime import datetime

from core.perception import PerceptionRuntime
from sources import metrics_probe


def _cfg(tmp_path, **over):
    cfg = {
        "command": """echo '{"metrics": {"a": 1, "b": 2.5}, "details": "gap notes"}'""",
        "name": "demo",
        "snapshot_hour": 0,
        "history_file": str(tmp_path / "demo.jsonl"),
    }
    cfg.update(over)
    return cfg


def _records(tmp_path, name="demo"):
    path = tmp_path / f"{name}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ── snapshot behavior ────────────────────────────────────────────────


def test_happy_path_snapshot_and_history(tmp_path):
    signals, state = metrics_probe.collect(_cfg(tmp_path), {})
    assert len(signals) == 1
    sig = signals[0]
    today = datetime.now().strftime("%Y-%m-%d")
    assert sig["event_id"] == f"snapshot:{today}"
    assert "demo" in sig["title"]
    assert "a: 1" in sig["body"] and "b: 2.5" in sig["body"]
    assert "gap notes" in sig["body"]
    assert sig["payload"]["metrics"] == {"a": 1, "b": 2.5}
    recs = _records(tmp_path)
    assert len(recs) == 1 and recs[0]["kind"] == "snapshot"
    assert recs[0]["metrics"] == {"a": 1, "b": 2.5}
    assert recs[0]["details"] == "gap notes"
    assert state["last_snapshot_date"] == today
    assert state["prev_snapshot"]["metrics"] == {"a": 1, "b": 2.5}


def test_snapshot_once_per_day(tmp_path):
    _, state = metrics_probe.collect(_cfg(tmp_path), {})
    signals, state = metrics_probe.collect(_cfg(tmp_path), state)
    assert signals == []  # same day: no second snapshot
    assert len(_records(tmp_path)) == 1


def test_snapshot_gated_by_hour(tmp_path, monkeypatch):
    fixed = datetime.now().astimezone().replace(hour=8)
    monkeypatch.setattr(metrics_probe, "_now", lambda: fixed)
    signals, state = metrics_probe.collect(
        _cfg(tmp_path, snapshot_hour=9), {})
    assert signals == []  # before snapshot_hour
    assert state["last_snapshot_date"] is None
    # baseline still recorded so tomorrow has deltas
    assert state["prev_snapshot"]["metrics"] == {"a": 1, "b": 2.5}


def test_deltas_vs_previous_snapshot(tmp_path):
    seeded = {"prev_snapshot": {"date": "2000-01-01", "metrics": {"a": 0.5}},
              "last_snapshot_date": "2000-01-01"}
    signals, _ = metrics_probe.collect(_cfg(tmp_path), seeded)
    assert signals[0]["payload"]["deltas"] == {"a": 0.5}  # 1 - 0.5
    assert "(+0.5)" in signals[0]["body"]
    assert _records(tmp_path)[0]["deltas"] == {"a": 0.5}


# ── anomaly rules ────────────────────────────────────────────────────


def test_rule_trips_with_stable_event_id(tmp_path):
    cfg = _cfg(tmp_path, rules=[{"metric": "a", "op": "<=", "value": 5}])
    signals, state = metrics_probe.collect(cfg, {})
    anomalies = [s for s in signals if s["event_id"].startswith("anomaly:")]
    assert len(anomalies) == 1
    today = datetime.now().strftime("%Y-%m-%d")
    assert anomalies[0]["event_id"] == f"anomaly:a:{today}"
    assert "🚨" in anomalies[0]["title"]
    # re-collect same day: same stable id (runtime seen-store dedups delivery)
    signals2, _ = metrics_probe.collect(cfg, state)
    again = [s for s in signals2 if s["event_id"].startswith("anomaly:")]
    assert again and again[0]["event_id"] == anomalies[0]["event_id"]
    kinds = [r["kind"] for r in _records(tmp_path)]
    assert kinds.count("anomaly") == 2  # history logs each evaluation


def test_rule_pct_of_prev(tmp_path):
    cfg = _cfg(tmp_path, rules=[{"metric": "a", "op": "<", "pct_of_prev": 50}])
    # no previous snapshot → pct rule silently skipped
    signals, state = metrics_probe.collect(cfg, {})
    assert not [s for s in signals if s["event_id"].startswith("anomaly:")]
    # prev a=10, current a=1 → 1 < 50% of 10 → trip
    seeded = {"prev_snapshot": {"date": "2000-01-01", "metrics": {"a": 10}},
              "last_snapshot_date": datetime.now().strftime("%Y-%m-%d")}
    signals, _ = metrics_probe.collect(cfg, seeded)
    anomalies = [s for s in signals if s["event_id"].startswith("anomaly:")]
    assert len(anomalies) == 1 and "50" in anomalies[0]["title"]


def test_rule_min_hour_gating(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, rules=[{"metric": "a", "op": "<=", "value": 5,
                                 "min_hour": 14}])
    fixed = datetime.now().astimezone().replace(hour=10)
    monkeypatch.setattr(metrics_probe, "_now", lambda: fixed)
    signals, _ = metrics_probe.collect(cfg, {})
    assert not [s for s in signals if s["event_id"].startswith("anomaly:")]
    fixed = fixed.replace(hour=15)
    monkeypatch.setattr(metrics_probe, "_now", lambda: fixed)
    signals, _ = metrics_probe.collect(cfg, {})
    assert [s for s in signals if s["event_id"].startswith("anomaly:")]


def test_malformed_rule_is_skipped_not_fatal(tmp_path):
    cfg = _cfg(tmp_path, rules=["garbage", {"metric": "a"},
                                {"metric": "a", "op": "~", "value": 1},
                                {"metric": "missing", "op": "<", "value": 1}])
    signals, state = metrics_probe.collect(cfg, {})
    assert len(signals) == 1  # just the snapshot; no crash, no anomalies
    assert state.get("error_type") is None


# ── failure paths (never raise) ──────────────────────────────────────


def test_nonzero_exit_is_network_error(tmp_path):
    prev = {"prev_snapshot": {"date": "2000-01-01", "metrics": {"a": 7}}}
    signals, state = metrics_probe.collect(
        _cfg(tmp_path, command="exit 3"), dict(prev))
    assert signals == []
    assert state["error_type"] == "network"
    assert state["prev_snapshot"]["metrics"] == {"a": 7}  # state preserved
    assert _records(tmp_path) == []


def test_garbage_stdout_is_crash_error(tmp_path):
    for cmd in ("echo not-json", """echo '{"nope": 1}'""", "true"):
        signals, state = metrics_probe.collect(_cfg(tmp_path, command=cmd), {})
        assert signals == [] and state["error_type"] == "crash"


def test_timeout_error(tmp_path):
    start = time.time()
    signals, state = metrics_probe.collect(
        _cfg(tmp_path, command="sleep 5", timeout=0.2), {})
    assert time.time() - start < 3
    assert signals == [] and state["error_type"] == "timeout"


def test_missing_command_is_crash(tmp_path):
    signals, state = metrics_probe.collect({"name": "x"}, {})
    assert signals == [] and state["error_type"] == "crash"


# ── hygiene: the command (may contain hosts) never leaks ─────────────


def test_command_string_never_in_signals_or_history(tmp_path):
    marker = "SECRETHOST-xyz.internal"
    cfg = _cfg(tmp_path,
               command=f"""true '{marker}'; echo '{{"metrics": {{"a": 1}}}}'""",
               rules=[{"metric": "a", "op": "<=", "value": 5}])
    signals, _ = metrics_probe.collect(cfg, {})
    assert signals
    assert marker not in json.dumps(signals, ensure_ascii=False)
    assert marker not in (tmp_path / "demo.jsonl").read_text()


# ── validate_cfg ─────────────────────────────────────────────────────


def test_validate_cfg():
    assert metrics_probe.validate_cfg(_ok_cfg()) == []
    assert any("command" in e for e in metrics_probe.validate_cfg({}))
    assert any(".op" in e for e in metrics_probe.validate_cfg(
        _ok_cfg(rules=[{"metric": "a", "op": "~", "value": 1}])))
    assert any("value or pct_of_prev" in e for e in metrics_probe.validate_cfg(
        _ok_cfg(rules=[{"metric": "a", "op": "<"}])))
    assert any("metric" in e for e in metrics_probe.validate_cfg(
        _ok_cfg(rules=[{"op": "<", "value": 1}])))
    assert any("snapshot_hour" in e for e in metrics_probe.validate_cfg(
        _ok_cfg(snapshot_hour=99)))


def _ok_cfg(**over):
    cfg = {"command": "echo hi", "rules": []}
    cfg.update(over)
    return cfg


# ── pipeline: runtime dedups the stable anomaly id ───────────────────


SOURCES_PROBE = """
perception:
  defaults: {sensitivity: internal, interval: 1s}
  sources:
    - id: probe
      type: metrics_probe
      collect:
        name: pipedemo
        command: "echo '{\\"metrics\\": {\\"a\\": 1}}'"
        snapshot_hour: 0
        history_file: "%s"
        rules:
          - {metric: a, op: "<=", value: 5}
      schedule: {interval: 1s}
      perceive: {buffer: inbox_ops.md}
"""


def test_pipeline_anomaly_delivered_once_per_day(tmp_path):
    (tmp_path / "sources.yaml").write_text(
        SOURCES_PROBE % (tmp_path / "pipedemo.jsonl"))
    memory = tmp_path / "memory"
    (memory / "system").mkdir(parents=True)
    rt = PerceptionRuntime(tmp_path, memory)

    summary = rt.run_collect()
    assert "collected=2" in summary  # snapshot + anomaly
    time.sleep(1.1)
    summary = rt.run_collect()
    assert "collected=0" in summary  # same day: anomaly re-emitted, deduped
    inbox = (rt.system_dir / "inbox_ops.md").read_text()
    assert inbox.count("anomaly:a:") == 1
    assert inbox.count("snapshot:") == 1


# ── rules v2: baseline + recovery ────────────────────────────────────


def _seed_history(tmp_path, days_vals, name="demo"):
    """Write snapshot records for the last len(days_vals) days (oldest first)."""
    from datetime import datetime, timedelta
    path = tmp_path / f"{name}.jsonl"
    now = datetime.now().astimezone()
    with open(path, "a", encoding="utf-8") as f:
        for i, v in enumerate(days_vals):
            d = (now - timedelta(days=len(days_vals) - i)).strftime("%Y-%m-%d")
            f.write(json.dumps({"ts": f"{d}T10:00:00+08:00", "date": d,
                                "kind": "snapshot", "name": name,
                                "metrics": {"a": v}}) + "\n")


def test_rule_pct_of_baseline_trips_on_slow_decline(tmp_path):
    # 7-day baseline mean = 10; current a=1 < 30% of baseline → trip.
    _seed_history(tmp_path, [10, 10, 10, 10])
    cfg = _cfg(tmp_path, rules=[{"metric": "a", "op": "<", "pct_of_baseline": 30}])
    signals, _ = metrics_probe.collect(cfg, {"last_snapshot_date":
                                             datetime.now().strftime("%Y-%m-%d")})
    anomalies = [s for s in signals if s["event_id"].startswith("anomaly:")]
    assert len(anomalies) == 1 and "baseline" in anomalies[0]["title"]


def test_rule_pct_of_baseline_silent_without_enough_history(tmp_path):
    _seed_history(tmp_path, [10, 10])  # only 2 days < BASELINE_MIN_DAYS
    cfg = _cfg(tmp_path, rules=[{"metric": "a", "op": "<", "pct_of_baseline": 30}])
    signals, _ = metrics_probe.collect(cfg, {"last_snapshot_date":
                                             datetime.now().strftime("%Y-%m-%d")})
    assert not [s for s in signals if s["event_id"].startswith("anomaly:")]


def test_recovery_emitted_once_after_previous_day_trip(tmp_path):
    today = datetime.now().strftime("%Y-%m-%d")
    cfg = _cfg(tmp_path, rules=[{"metric": "a", "op": ">", "value": 100}])
    # a=1 doesn't trip; metric tripped YESTERDAY per state; snapshot due.
    state = {"tripped": {"a": "2000-01-01"}}
    signals, new_state = metrics_probe.collect(cfg, state)
    recoveries = [s for s in signals if s["event_id"].startswith("recovery:")]
    assert len(recoveries) == 1
    assert recoveries[0]["event_id"] == f"recovery:a:{today}"
    assert "✅" in recoveries[0]["title"]
    assert "a" not in (new_state.get("tripped") or {})
    kinds = [r["kind"] for r in _records(tmp_path)]
    assert kinds.count("recovery") == 1


def test_no_recovery_when_rule_gated_by_min_hour(tmp_path, monkeypatch):
    # Rule gated to the afternoon: a morning snapshot must NOT declare the
    # metric recovered — it was never evaluated. The trip marker survives
    # (yesterday's date — recent enough to escape the 7-day GC).
    from datetime import timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    fixed = datetime.now().astimezone().replace(hour=10)
    monkeypatch.setattr(metrics_probe, "_now", lambda: fixed)
    cfg = _cfg(tmp_path, rules=[{"metric": "a", "op": "<=", "value": 0,
                                 "min_hour": 14}])
    state = {"tripped": {"a": yesterday}}
    signals, new_state = metrics_probe.collect(cfg, state)
    assert not [s for s in signals if s["event_id"].startswith("recovery:")]
    assert (new_state.get("tripped") or {}).get("a") == yesterday  # kept


def test_same_day_trip_does_not_recover(tmp_path):
    # tripped earlier TODAY and clean now → no recovery (needs a prior day).
    today = datetime.now().strftime("%Y-%m-%d")
    cfg = _cfg(tmp_path, rules=[{"metric": "a", "op": ">", "value": 100}])
    signals, _ = metrics_probe.collect(cfg, {"tripped": {"a": today}})
    assert not [s for s in signals if s["event_id"].startswith("recovery:")]


def test_dry_run_env_skips_history(tmp_path, monkeypatch):
    monkeypatch.setenv("PERCEPTION_DRY_RUN", "1")
    signals, _ = metrics_probe.collect(_cfg(tmp_path), {})
    assert signals  # snapshot still emitted for inspection
    assert _records(tmp_path) == []  # but nothing persisted
