"""Tests for the metrics-digest pre/post pipeline (watermark handshake)."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PRE = REPO / "tasks" / "metrics_digest_pre.sh"
POST = REPO / "tasks" / "metrics_digest_post.py"


def _env(tmp_path):
    import os
    return {**os.environ, "JARVIS_DIR": str(tmp_path)}


def _write_records(tmp_path, records, name="demo"):
    mdir = tmp_path / "data" / "metrics"
    mdir.mkdir(parents=True, exist_ok=True)
    with open(mdir / f"{name}.jsonl", "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return mdir


def _run_pre(tmp_path):
    return subprocess.run(["bash", str(PRE)], env=_env(tmp_path),
                          capture_output=True, text=True, timeout=30)


def _run_post(tmp_path, payload):
    return subprocess.run([sys.executable, str(POST)], input=payload,
                          env=_env(tmp_path),
                          capture_output=True, text=True, timeout=30)


SNAP = {"ts": "2026-07-15T10:00:00+08:00", "date": "2026-07-15",
        "kind": "snapshot", "name": "demo", "metrics": {"a": 1},
        "deltas": {"a": 0.5}, "details": "", "digest_hint": "说人话"}
ANOM = {"ts": "2026-07-15T14:00:00+08:00", "date": "2026-07-15",
        "kind": "anomaly", "name": "demo", "metric": "a",
        "rule": {"metric": "a", "op": "<=", "value": 0}, "actual": 0,
        "threshold": 0, "digest_hint": ""}


# ── pre ──────────────────────────────────────────────────────────────


def test_pre_no_data_emits_nothing(tmp_path):
    r = _run_pre(tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == ""
    assert not (tmp_path / "data" / "metrics" / ".digest_pending.json").exists()


def test_pre_emits_records_and_stages_pending(tmp_path):
    mdir = _write_records(tmp_path, [SNAP, ANOM])
    r = _run_pre(tmp_path)
    assert "=== METRICS RECORDS ===" in r.stdout
    assert '"kind": "snapshot"' in r.stdout and '"kind": "anomaly"' in r.stdout
    assert "说人话" in r.stdout
    pending = json.load(open(mdir / ".digest_pending.json"))
    assert pending["ts"] == ANOM["ts"]  # max emitted ts


def test_pre_respects_watermark(tmp_path):
    mdir = _write_records(tmp_path, [SNAP, ANOM])
    (mdir / ".digest_watermark.json").write_text(
        json.dumps({"ts": SNAP["ts"]}))
    r = _run_pre(tmp_path)
    assert '"kind": "snapshot"' not in r.stdout  # at watermark → already sent
    assert '"kind": "anomaly"' in r.stdout       # newer → emitted


# ── post ─────────────────────────────────────────────────────────────


def _stage_pending(tmp_path, ts="2026-07-15T14:00:00+08:00"):
    mdir = tmp_path / "data" / "metrics"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / ".digest_pending.json").write_text(json.dumps({"ts": ts}))
    return mdir


def test_post_renders_cards_and_promotes(tmp_path):
    mdir = _stage_pending(tmp_path)
    payload = json.dumps({"cards": [
        {"header": "📈 demo 日报", "body": "新增 1（比昨天多 0.5）"},
        {"header": "🚨 demo 异常", "body": "a 掉到 0"},
    ]}, ensure_ascii=False)
    r = _run_post(tmp_path, payload)
    assert r.returncode == 0
    cards = [l for l in r.stdout.splitlines() if l.startswith('{"config":')]
    assert len(cards) == 2
    parsed = json.loads(cards[0])
    assert "demo 日报" in json.dumps(parsed, ensure_ascii=False)
    assert not (mdir / ".digest_pending.json").exists()
    wm = json.load(open(mdir / ".digest_watermark.json"))
    assert wm["ts"] == "2026-07-15T14:00:00+08:00"


def test_post_heartbeat_ok_promotes_without_cards(tmp_path):
    mdir = _stage_pending(tmp_path)
    r = _run_post(tmp_path, "HEARTBEAT_OK")
    assert r.returncode == 0 and r.stdout.strip() == ""
    assert (mdir / ".digest_watermark.json").exists()
    assert not (mdir / ".digest_pending.json").exists()


def test_post_no_envelope_keeps_pending_for_retry(tmp_path):
    mdir = _stage_pending(tmp_path)
    for payload in ("just some prose, no JSON here",
                    "Execution error: something broke"):
        r = _run_post(tmp_path, payload)
        assert r.returncode == 0 and r.stdout.strip() == ""
        assert (mdir / ".digest_pending.json").exists()   # NOT promoted
        assert not (mdir / ".digest_watermark.json").exists()


def test_post_malformed_cards_are_skipped(tmp_path):
    _stage_pending(tmp_path)
    payload = json.dumps({"cards": ["garbage", {"header": "", "body": "x"},
                                    {"header": "ok", "body": "fine"}]})
    r = _run_post(tmp_path, payload)
    cards = [l for l in r.stdout.splitlines() if l.startswith('{"config":')]
    assert len(cards) == 1


# ── end-to-end: failed cycle re-emits, successful cycle advances ─────


def test_failed_cycle_reemits_then_advances(tmp_path):
    _write_records(tmp_path, [SNAP])
    r1 = _run_pre(tmp_path)
    assert '"kind": "snapshot"' in r1.stdout
    _run_post(tmp_path, "__NO_ENVELOPE__ garbled")
    r2 = _run_pre(tmp_path)
    assert '"kind": "snapshot"' in r2.stdout  # re-emitted after failure
    _run_post(tmp_path, "HEARTBEAT_OK")
    r3 = _run_pre(tmp_path)
    assert r3.stdout.strip() == ""            # watermark advanced


def test_post_prose_plus_trailing_sentinel_promotes(tmp_path):
    # Sentinel anywhere = model chose silence; that's valid — promote the
    # watermark instead of re-emitting the records forever. And nothing may
    # reach stdout (the 2026-07-15 leak shape).
    mdir = _stage_pending(tmp_path)
    r = _run_post(tmp_path, "records look routine, nothing to send\n\nHEARTBEAT_OK")
    assert r.stdout.strip() == ""
    assert (mdir / ".digest_watermark.json").exists()
    assert not (mdir / ".digest_pending.json").exists()


# ── absence alerts (PRD-1: silence must not look like health) ────────


def _write_sources_yaml(tmp_path, snapshot_hour=9, name="demo"):
    (tmp_path / "sources.yaml").write_text(f"""
perception:
  sources:
    - id: {name}
      type: metrics_probe
      enabled: true
      collect:
        name: {name}
        command: "echo x"
        snapshot_hour: {snapshot_hour}
        history_file: "{tmp_path}/data/metrics/{name}.jsonl"
""")


def _now_at(hour):
    from datetime import datetime
    return datetime.now().astimezone().replace(hour=hour, minute=0)


def test_absence_alert_when_snapshot_overdue(tmp_path):
    from core.metrics_digest import main
    _write_sources_yaml(tmp_path, snapshot_hour=9)
    out = main(tmp_path, now=_now_at(12))  # 9 + 2h grace < 12
    assert '"kind": "absence"' in out
    assert '"name": "demo"' in out
    # once per day: second call stays silent
    assert main(tmp_path, now=_now_at(13)) == ""


def test_no_absence_before_grace(tmp_path):
    from core.metrics_digest import main
    _write_sources_yaml(tmp_path, snapshot_hour=9)
    assert main(tmp_path, now=_now_at(10)) == ""  # inside 2h grace


def test_no_absence_when_snapshot_exists(tmp_path):
    from datetime import datetime
    from core.metrics_digest import main
    _write_sources_yaml(tmp_path, snapshot_hour=9)
    today = datetime.now().strftime("%Y-%m-%d")
    _write_records(tmp_path, [{"ts": f"{today}T09:30:00+08:00", "date": today,
                               "kind": "snapshot", "name": "demo",
                               "metrics": {"a": 1}}])
    out = main(tmp_path, now=_now_at(12))
    assert '"kind": "absence"' not in out
    assert '"kind": "snapshot"' in out  # the real record still emits


def test_absence_alone_does_not_stage_pending(tmp_path):
    from core.metrics_digest import main
    _write_sources_yaml(tmp_path, snapshot_hour=9)
    main(tmp_path, now=_now_at(12))
    # absence records are synthetic — nothing to watermark
    assert not (tmp_path / "data" / "metrics" / ".digest_pending.json").exists()


def test_disabled_probe_never_alerts_absence(tmp_path):
    from core.metrics_digest import main
    (tmp_path / "sources.yaml").write_text("""
perception:
  sources:
    - id: demo
      type: metrics_probe
      enabled: false
      collect: {name: demo, command: "echo x", snapshot_hour: 0}
""")
    assert main(tmp_path, now=_now_at(23)) == ""
