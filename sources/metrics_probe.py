"""metrics_probe adapter — run a configured command that prints metrics JSON.

Generic "pull numbers from YOUR infrastructure" connector. The command (and
any SSH host, SQL, or file path inside it) lives in gitignored sources.yaml /
data/probes/ — never in tracked code, and never echoed into signals or
history records.

Command contract — print ONE JSON object to stdout:

    {"metrics": {"<name>": <number>, ...}, "details": "<free text, optional>"}

Extra keys are ignored. Failure classification (never raises, per the
adapter contract): timeout → "timeout"; non-zero exit → "network";
unparsable/shapeless stdout → "crash".

Per collect: run command → day-over-day deltas vs the previous snapshot
(kept in state, one snapshot only) → evaluate anomaly rules → emit at most
one daily snapshot signal (at/after cfg snapshot_hour) plus one signal per
tripped rule. Snapshot/anomaly records are appended to a history jsonl that
the metrics-digest heartbeat task turns into cards; the per-user rendering
guidance travels with each record as cfg digest_hint.

Anomaly event_ids are "anomaly:<metric>:<date>" — stable, so the runtime
seen-store yields at most one alert per rule per day without any dedup
logic here.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

ALLOWED_OPS = ("<", "<=", ">", ">=", "==", "!=")
DETAILS_RECORD_CAP = 4000
DETAILS_BODY_CAP = 1000
DEFAULT_TIMEOUT = 45


def _now() -> datetime:
    """Local now — module-level so tests can monkeypatch the clock."""
    return datetime.now().astimezone()


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt(v: float) -> str:
    return f"{v:g}"


def _history_path(cfg: dict, name: str) -> Path:
    raw = cfg.get("history_file") or os.path.join("data", "metrics", f"{name}.jsonl")
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_absolute():
        path = Path(os.environ.get("JARVIS_DIR", ".")) / path
    return path


def _append_history(path: Path, record: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # history is best-effort; the signal still reaches the inbox


def _run_command(command: str, timeout: float) -> tuple[dict | None, str | None]:
    """(parsed payload, error_type). Exactly one side is non-None."""
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True, timeout=timeout,
            cwd=os.environ.get("JARVIS_DIR") or None)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception:
        return None, "crash"
    if proc.returncode != 0:
        return None, "network"
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None, "crash"
    if not isinstance(payload, dict) or not isinstance(payload.get("metrics"), dict):
        return None, "crash"
    return payload, None


def _eval_rules(rules: list, metrics: dict, prev_metrics: dict,
                now: datetime) -> list[dict]:
    """Tripped rules → [{metric, op, threshold, threshold_desc, actual, rule}]."""
    import operator
    ops = {"<": operator.lt, "<=": operator.le, ">": operator.gt,
           ">=": operator.ge, "==": operator.eq, "!=": operator.ne}
    tripped = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        metric, op = rule.get("metric"), rule.get("op")
        if not metric or op not in ops:
            continue
        min_hour = rule.get("min_hour")
        if isinstance(min_hour, (int, float)) and now.hour < min_hour:
            continue
        actual = metrics.get(metric)
        if not _is_number(actual):
            continue
        pct = rule.get("pct_of_prev")
        if _is_number(pct):
            prev = prev_metrics.get(metric)
            if not _is_number(prev) or prev == 0:
                continue  # no baseline yet — pct rules need a previous snapshot
            threshold = prev * pct / 100.0
            desc = f"{_fmt(pct)}% of prev ({_fmt(threshold)})"
        elif _is_number(rule.get("value")):
            threshold = rule["value"]
            desc = _fmt(threshold)
        else:
            continue
        if ops[op](actual, threshold):
            tripped.append({"metric": metric, "op": op, "threshold": threshold,
                            "threshold_desc": desc, "actual": actual,
                            "rule": {k: rule[k] for k in
                                     ("metric", "op", "value", "pct_of_prev", "min_hour")
                                     if k in rule}})
    return tripped


def collect(cfg: dict, state: dict) -> tuple[list[dict], dict]:
    command = cfg.get("command")
    if not isinstance(command, str) or not command.strip():
        return [], {**state, "error_type": "crash"}
    name = str(cfg.get("name") or "metrics")
    timeout = cfg.get("timeout", DEFAULT_TIMEOUT)
    if not _is_number(timeout) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT
    snapshot_hour = cfg.get("snapshot_hour", 9)
    if not _is_number(snapshot_hour):
        snapshot_hour = 9
    digest_hint = str(cfg.get("digest_hint") or "").strip()

    payload, err = _run_command(command, timeout)
    if err:
        return [], {**state, "error_type": err}

    metrics = {k: v for k, v in payload["metrics"].items() if _is_number(v)}
    details = str(payload.get("details") or "").strip()

    now = _now()
    today = now.strftime("%Y-%m-%d")
    ts = now.isoformat(timespec="seconds")
    prev = state.get("prev_snapshot") or {}
    prev_metrics = prev.get("metrics") or {}
    deltas = {k: round(v - prev_metrics[k], 2) for k, v in metrics.items()
              if _is_number(prev_metrics.get(k))}
    history = _history_path(cfg, name)
    signals: list[dict] = []
    new_state = {"prev_snapshot": prev or None,
                 "last_snapshot_date": state.get("last_snapshot_date")}

    for hit in _eval_rules(cfg.get("rules") or [], metrics, prev_metrics, now):
        title = (f"🚨 {name} 异常: {hit['metric']} {hit['op']} "
                 f"{hit['threshold_desc']} (当前 {_fmt(hit['actual'])})")
        signals.append({
            "event_id": f"anomaly:{hit['metric']}:{today}",
            "ts": ts,
            "title": title,
            "summary": title,
            "body": f"规则: {hit['metric']} {hit['op']} {hit['threshold_desc']}\n"
                    f"当前值: {_fmt(hit['actual'])}",
            "actor": {"raw": "", "resolved": ""},
            "payload": {"kind": "anomaly", "metric": hit["metric"],
                        "actual": hit["actual"]},
        })
        _append_history(history, {
            "ts": ts, "date": today, "kind": "anomaly", "name": name,
            "metric": hit["metric"], "rule": hit["rule"], "actual": hit["actual"],
            "threshold": hit["threshold"], "digest_hint": digest_hint,
        })

    snapshot_due = (now.hour >= snapshot_hour
                    and state.get("last_snapshot_date") != today)
    if snapshot_due:
        lines = []
        for k in sorted(metrics):
            d = deltas.get(k)
            lines.append(f"{k}: {_fmt(metrics[k])}"
                         + (f" ({d:+g})" if d is not None else ""))
        if details:
            lines += ["", details[:DETAILS_BODY_CAP]]
        summary = "  ".join(
            f"{k}={_fmt(metrics[k])}"
            + (f"({deltas[k]:+g})" if deltas.get(k) is not None else "")
            for k in sorted(metrics)[:5]) or "no metrics"
        signals.append({
            "event_id": f"snapshot:{today}",
            "ts": ts,
            "title": f"📈 {name} 日快照 {today}",
            "summary": summary,
            "body": "\n".join(lines),
            "actor": {"raw": "", "resolved": ""},
            "payload": {"kind": "snapshot", "metrics": metrics, "deltas": deltas},
        })
        _append_history(history, {
            "ts": ts, "date": today, "kind": "snapshot", "name": name,
            "metrics": metrics, "deltas": deltas,
            "details": details[:DETAILS_RECORD_CAP], "digest_hint": digest_hint,
        })
        new_state["last_snapshot_date"] = today
        new_state["prev_snapshot"] = {"date": today, "metrics": metrics}

    if not new_state.get("prev_snapshot"):
        # First successful probe: baseline silently so day-over-day deltas
        # and pct_of_prev rules have something to compare against tomorrow.
        new_state["prev_snapshot"] = {"date": today, "metrics": metrics}
    return signals, new_state


def validate_cfg(cfg: dict) -> list[str]:
    errs = []
    command = cfg.get("command")
    if not isinstance(command, str) or not command.strip():
        errs.append("command: required non-empty string")
    rules = cfg.get("rules")
    if rules is not None and not isinstance(rules, list):
        errs.append("rules: must be a list")
    for i, rule in enumerate(rules or [] if isinstance(rules, list) else []):
        if not isinstance(rule, dict):
            errs.append(f"rules[{i}]: must be a mapping")
            continue
        if not rule.get("metric"):
            errs.append(f"rules[{i}].metric: required")
        if rule.get("op") not in ALLOWED_OPS:
            errs.append(f"rules[{i}].op: must be one of {', '.join(ALLOWED_OPS)}")
        if not (_is_number(rule.get("value")) or _is_number(rule.get("pct_of_prev"))):
            errs.append(f"rules[{i}]: needs numeric value or pct_of_prev")
    hour = cfg.get("snapshot_hour")
    if hour is not None and (not _is_number(hour) or not 0 <= hour <= 23):
        errs.append("snapshot_hour: must be 0-23")
    return errs
