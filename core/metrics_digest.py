"""metrics-digest pre logic — emit undigested probe records + absence alerts.

Called by tasks/metrics_digest_pre.sh (`python3 -m core.metrics_digest`).
Kept as a module so the time-dependent absence logic is unit-testable.

Two jobs:
1. Emit metrics_probe history records (data/metrics/*.jsonl) newer than the
   digest watermark, staging the candidate watermark in .digest_pending.json
   (promoted by the post-hook only on a well-formed Claude reply).
   Steady-state kind=snapshot records are dropped here (REQ-121): the
   history files are the durable 台账 and only state flips
   (anomaly/recovery/absence) are worth a card.
2. Absence alerts: a probe that failed all day produces NO records — and a
   watermark can't miss what never arrived (2026-07-15: the server-side
   improvement loop was dead for three weeks and the old daily card kept
   rendering stale data; silence must not look like health). For every
   enabled metrics_probe source whose snapshot_hour + grace has passed with
   no snapshot record today, emit one synthetic kind=absence record (once
   per source per day, tracked in .absence_alerted.json).

Empty stdout (no records, no absences) = the heartbeat skips the task.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from pathlib import Path

MAX_RECORDS = 12          # bound the prompt; older overflow re-emits next cycle
ABSENCE_GRACE_HOURS = 2   # alert only this long after snapshot_hour
REALERT_AFTER_H = 24      # ongoing anomaly reminder cadence (edge-triggered)


def _metrics_dir(jarvis_dir: Path) -> Path:
    return jarvis_dir / "data" / "metrics"


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def collect_new_records(jarvis_dir: Path) -> tuple[list[dict], str | None]:
    """(records newer than the watermark, candidate pending ts)."""
    mdir = _metrics_dir(jarvis_dir)
    watermark = (_load_json(mdir / ".digest_watermark.json", {}) or {}).get("ts") or ""
    records = []
    for path in sorted(glob.glob(str(mdir / "*.jsonl"))):
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            # ISO-8601 local timestamps from one machine compare correctly
            # as strings; a record without ts is malformed → skip.
            if isinstance(rec, dict) and rec.get("ts", "") > watermark:
                records.append(rec)
    records.sort(key=lambda r: r.get("ts", ""))
    records = records[:MAX_RECORDS]
    pending_ts = max((r["ts"] for r in records), default=None)
    return records, pending_ts


def filter_anomalies(jarvis_dir: Path, records: list[dict]) -> list[dict]:
    """Edge-trigger anomaly cards: first trip / value change / recovery only.

    Probes re-emit a still-true anomaly every poll cycle (level-triggered),
    which turned one ongoing FTC 503 outage into 8 identical 🚨 cards on
    7/19 alone. Track active anomalies in .anomaly_state.json keyed by
    name:metric; while the value is unchanged, repeats are suppressed except
    a once-per-REALERT_AFTER_H "still broken" reminder. A recovery record
    always passes and clears the key. Watermark advancement is unaffected —
    the caller computes pending_ts before this filter."""
    mdir = _metrics_dir(jarvis_dir)
    state_path = mdir / ".anomaly_state.json"
    state = _load_json(state_path, {}) or {}
    kept, changed = [], False
    for rec in records:
        kind = rec.get("kind")
        if kind not in ("anomaly", "recovery"):
            kept.append(rec)
            continue
        key = f"{rec.get('name', '')}:{rec.get('metric', '')}"
        if kind == "recovery":
            if state.pop(key, None) is not None:
                changed = True
            kept.append(rec)
            continue
        prev = state.get(key)
        ts = str(rec.get("ts", ""))
        # ts == alerted_ts means this is the SAME record that wrote the
        # state — a retry after a failed Claude render (post didn't promote
        # the watermark, so it re-collected). Always pass it: suppressing
        # here would let the direct-advance branch below skip a first alert
        # that never reached the user (red-team P1, 7/22).
        if (prev and prev.get("actual") == rec.get("actual")
                and ts != prev.get("alerted_ts")):
            try:
                prev_dt = datetime.fromisoformat(str(prev.get("alerted_ts")))
                cur_dt = datetime.fromisoformat(ts)
                still_fresh = (cur_dt - prev_dt).total_seconds() < REALERT_AFTER_H * 3600
            except (ValueError, TypeError):
                still_fresh = False  # unparsable state: err toward alerting
            if still_fresh:
                continue
        state[key] = {"actual": rec.get("actual"), "alerted_ts": ts}
        changed = True
        kept.append(rec)
    if changed:
        try:
            mdir.mkdir(parents=True, exist_ok=True)
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, state_path)
        except OSError:
            pass
    return kept


def _probe_sources(jarvis_dir: Path) -> list[dict]:
    """Enabled metrics_probe collect-blocks from sources.yaml (missing → [])."""
    path = jarvis_dir / "sources.yaml"
    if not path.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return []
    sources = ((data.get("perception") or {}).get("sources") or [])
    return [s.get("collect") or {} for s in sources
            if isinstance(s, dict) and s.get("type") == "metrics_probe"
            and s.get("enabled", True)]


def _has_snapshot_today(history: Path, today: str) -> bool:
    try:
        for line in history.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "snapshot" and rec.get("date") == today:
                return True
    except OSError:
        pass
    return False


def detect_absences(jarvis_dir: Path, now: datetime) -> list[dict]:
    """Synthetic kind=absence records for probes whose daily snapshot is
    overdue. At most one per source per day (.absence_alerted.json marker,
    written here — a lost alert on a failed Claude cycle simply re-fires
    tomorrow, which is an acceptable failure mode for a daily-cadence alarm)."""
    mdir = _metrics_dir(jarvis_dir)
    today = now.strftime("%Y-%m-%d")
    marker_path = mdir / ".absence_alerted.json"
    marker = _load_json(marker_path, {}) or {}
    absences = []
    for cfg in _probe_sources(jarvis_dir):
        name = str(cfg.get("name") or "metrics")
        hour = cfg.get("snapshot_hour", 9)
        if not isinstance(hour, (int, float)) or isinstance(hour, bool):
            hour = 9
        if now.hour < hour + ABSENCE_GRACE_HOURS:
            continue
        raw = cfg.get("history_file") or os.path.join("data", "metrics",
                                                      f"{name}.jsonl")
        history = Path(os.path.expanduser(str(raw)))
        if not history.is_absolute():
            history = jarvis_dir / history
        if _has_snapshot_today(history, today):
            continue
        if marker.get(name) == today:
            continue
        marker[name] = today
        absences.append({
            "kind": "absence", "name": name, "date": today,
            "expected_by": f"{int(hour + ABSENCE_GRACE_HOURS):02d}:00",
            "ts": now.isoformat(timespec="seconds"),
        })
    if absences:
        try:
            mdir.mkdir(parents=True, exist_ok=True)
            tmp = marker_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(marker, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, marker_path)
        except OSError:
            pass
    return absences


def main(jarvis_dir: str | Path | None = None,
         now: datetime | None = None) -> str:
    jarvis_dir = Path(jarvis_dir or os.environ.get("JARVIS_DIR", "."))
    now = now or datetime.now().astimezone()
    records, pending_ts = collect_new_records(jarvis_dir)
    # REQ-121 (2026-08-11 降噪): steady-state daily snapshots never become
    # cards — the probe history files under data/metrics/ ARE the 台账, and
    # 14 days of engagement data showed the daily report card was pure noise.
    # Only state FLIPS reach Claude: anomaly/recovery (already edge-triggered
    # against .anomaly_state.json in filter_anomalies) and absence (once per
    # source per day). The watermark still advances over the dropped
    # snapshots via pending_ts, computed before this filter.
    records = [r for r in records if r.get("kind") != "snapshot"]
    records = filter_anomalies(jarvis_dir, records)
    # 2026-08-13「下次直接去查别等了」: an anomaly whose source configures
    # `investigate` gets its read-only investigation run HERE, and the
    # findings travel inside the record (core/pgc_outage_probe.py).
    from core import pgc_outage_probe
    for rec in records:
        pgc_outage_probe.attach(jarvis_dir, _metrics_dir(jarvis_dir), rec, now)
    absences = detect_absences(jarvis_dir, now)
    if not records and not absences:
        # Everything collected was a suppressed repeat → the task is skipped
        # and the post-hook never promotes the pending watermark. Promote it
        # HERE, directly: otherwise the suppressed records re-collect forever
        # and (at MAX_RECORDS) crowd new snapshots out of the window. Safe —
        # nothing in this batch needed Claude to render.
        if pending_ts:
            mdir = _metrics_dir(jarvis_dir)
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / ".digest_watermark.json").write_text(
                json.dumps({"ts": pending_ts}), encoding="utf-8")
            # A stale pending from an earlier failed render would otherwise
            # be promoted later and rewind the watermark we just advanced.
            (mdir / ".digest_pending.json").unlink(missing_ok=True)
        return ""
    if pending_ts:
        mdir = _metrics_dir(jarvis_dir)
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / ".digest_pending.json").write_text(
            json.dumps({"ts": pending_ts}), encoding="utf-8")
    lines = ["=== METRICS RECORDS ==="]
    lines += [json.dumps(r, ensure_ascii=False) for r in records + absences]
    return "\n".join(lines)


if __name__ == "__main__":
    out = main()
    if out:
        print(out)
