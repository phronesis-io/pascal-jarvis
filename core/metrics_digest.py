"""metrics-digest pre logic — emit undigested probe records + absence alerts.

Called by tasks/metrics_digest_pre.sh (`python3 -m core.metrics_digest`).
Kept as a module so the time-dependent absence logic is unit-testable.

Two jobs:
1. Emit metrics_probe history records (data/metrics/*.jsonl) newer than the
   digest watermark, staging the candidate watermark in .digest_pending.json
   (promoted by the post-hook only on a well-formed Claude reply).
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
            marker_path.write_text(json.dumps(marker, ensure_ascii=False),
                                   encoding="utf-8")
        except OSError:
            pass
    return absences


def main(jarvis_dir: str | Path | None = None,
         now: datetime | None = None) -> str:
    jarvis_dir = Path(jarvis_dir or os.environ.get("JARVIS_DIR", "."))
    now = now or datetime.now().astimezone()
    records, pending_ts = collect_new_records(jarvis_dir)
    absences = detect_absences(jarvis_dir, now)
    if not records and not absences:
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
