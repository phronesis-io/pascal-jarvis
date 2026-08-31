"""metrics_probe adapter — run a configured command that prints metrics JSON.

Generic "pull numbers from YOUR infrastructure" connector. The command (and
any SSH host, SQL, or file path inside it) lives in gitignored sources.yaml /
data/probes/ — never in tracked code, and never echoed into signals or
history records.

Command contract — print ONE JSON object to stdout:

    {"metrics": {"<name>": <number>, ...}, "details": "<free text, optional>",
     "errors": {"<component>": "<what could not be read>", ...}}

``errors`` (optional) is how a probe says "I ran, but part of me is blind":
each component becomes a synthetic anomaly on metric ``blind:<component>``
whose value is the number of days it has been blind (edge-triggered like
any rule, so the card escalates「失明 N 天」daily until it clears), and one
recovery signal when the component reads again. 2026-08-25→31 the PGC
pulse silently reported ``broken_first_party=None`` for six days because
its metrics endpoint had moved; a metric that quietly disappears must
become a state flip, not a gap in the 台账. Other extra keys are ignored. Failure classification (never raises, per the
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

Rules support three threshold forms:
- value: absolute number
- pct_of_prev: percentage of the previous snapshot's value (day-over-day)
- pct_of_baseline (+ optional window_days, default 7): percentage of the
  mean over recent snapshot history — catches slow declines that
  day-over-day comparison never sees; silent until ≥3 days of history.
A metric that alarmed on a previous day and later evaluates clean emits one
"recovery:<metric>:<date>" signal at snapshot time.
"""

from __future__ import annotations

import json
import os
import signal
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
    if os.environ.get("PERCEPTION_DRY_RUN"):
        return  # a dry-run record would resurface as a duplicate digest card
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # history is best-effort; the signal still reaches the inbox


def _run_command(command: str, timeout: float) -> tuple[dict | None, str | None]:
    """(parsed payload, error_type). Exactly one side is non-None."""
    try:
        proc = subprocess.Popen(
            ["bash", "-c", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
            cwd=os.environ.get("JARVIS_DIR") or None)
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return None, "timeout"
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            return None, "timeout"
    except Exception:
        return None, "crash"
    if proc.returncode != 0:
        return None, "network"
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None, "crash"
    if not isinstance(payload, dict) or not isinstance(payload.get("metrics"), dict):
        return None, "crash"
    return payload, None


BASELINE_MIN_DAYS = 3        # pct_of_baseline needs at least this many days
DEFAULT_BASELINE_WINDOW = 7  # days of snapshot history for the baseline mean


def _baseline_mean(history: Path, metric: str, window_days: int,
                   today: str, now: datetime):
    """Mean of `metric` over snapshot records from the last `window_days`
    days (excluding today). None until ≥ BASELINE_MIN_DAYS values exist —
    an unripe baseline must not fire alarms."""
    from datetime import timedelta
    cutoff = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")
    vals = []
    try:
        for line in history.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            date = rec.get("date", "")
            if rec.get("kind") != "snapshot" or not (cutoff <= date < today):
                continue
            v = (rec.get("metrics") or {}).get(metric)
            if _is_number(v):
                vals.append(v)
    except OSError:
        return None
    return sum(vals) / len(vals) if len(vals) >= BASELINE_MIN_DAYS else None


def _eval_rules(rules: list, metrics: dict, prev_metrics: dict,
                now: datetime, history: Path,
                today: str) -> tuple[list[dict], set]:
    """(tripped hits, metrics that were EVALUATED and came back clean).

    The clean set only contains metrics whose rules actually ran this pass —
    a rule skipped by min_hour or a missing baseline must not count as
    "recovered" (a daily-reset metric like new_today gated to the afternoon
    would otherwise look healed every morning).
    """
    import operator
    ops = {"<": operator.lt, "<=": operator.le, ">": operator.gt,
           ">=": operator.ge, "==": operator.eq, "!=": operator.ne}
    tripped = []
    evaluated_clean = set()
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
        pct_prev = rule.get("pct_of_prev")
        pct_base = rule.get("pct_of_baseline")
        if _is_number(pct_prev):
            prev = prev_metrics.get(metric)
            if not _is_number(prev) or prev == 0:
                continue  # no baseline yet — pct rules need a previous snapshot
            threshold = prev * pct_prev / 100.0
            desc = f"{_fmt(pct_prev)}% of prev ({_fmt(threshold)})"
        elif _is_number(pct_base):
            window = rule.get("window_days")
            if not _is_number(window) or window <= 0:
                window = DEFAULT_BASELINE_WINDOW
            base = _baseline_mean(history, metric, int(window), today, now)
            if base is None or base == 0:
                continue  # not enough history yet — stay silent, not wrong
            threshold = base * pct_base / 100.0
            desc = (f"{_fmt(pct_base)}% of {int(window)}d baseline "
                    f"({_fmt(threshold)})")
        elif _is_number(rule.get("value")):
            threshold = rule["value"]
            desc = _fmt(threshold)
        else:
            continue
        if ops[op](actual, threshold):
            tripped.append({"metric": metric, "op": op, "threshold": threshold,
                            "threshold_desc": desc, "actual": actual,
                            "rule": {k: rule[k] for k in
                                     ("metric", "op", "value", "pct_of_prev",
                                      "pct_of_baseline", "window_days", "min_hour")
                                     if k in rule}})
        else:
            evaluated_clean.add(metric)
    # a metric that tripped ANY rule is not clean, whatever other rules said
    evaluated_clean -= {h["metric"] for h in tripped}
    return tripped, evaluated_clean


ERROR_MESSAGE_CAP = 200


def _payload_errors(payload: dict) -> dict[str, str]:
    raw = payload.get("errors")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for component, message in raw.items():
        component = str(component or "").strip()
        message = str(message or "").strip()
        if component and message:
            out[component] = message[:ERROR_MESSAGE_CAP]
    return out


def _blind_components(errors: dict, prev_since, now: datetime, ts: str):
    """(synthetic hits for currently-blind components, [(component, since)]
    for ones that just recovered, new blind_since state)."""
    since_map = dict(prev_since) if isinstance(prev_since, dict) else {}
    hits = []
    for component, message in errors.items():
        since = str(since_map.get(component) or ts)
        since_map[component] = since
        try:
            first = datetime.fromisoformat(since)
            if first.tzinfo is None:
                first = first.replace(tzinfo=now.tzinfo)
            days = max(1, (now.date() - first.astimezone(now.tzinfo).date()).days + 1)
        except ValueError:
            days = 1
        metric = f"blind:{component}"
        hits.append({"metric": metric, "op": ">=", "threshold": 1,
                     "threshold_desc": "1", "actual": days,
                     "rule": {"metric": metric, "op": ">=", "value": 1},
                     "component": component, "error": message, "since": since})
    recovered = [(c, s) for c, s in since_map.items() if c not in errors]
    for component, _ in recovered:
        del since_map[component]
    return hits, recovered, since_map


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
    errors = _payload_errors(payload)

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

    tripped, evaluated_clean = _eval_rules(
        cfg.get("rules") or [], metrics, prev_metrics, now, history, today)
    blind_hits, blind_recovered, new_state["blind_since"] = _blind_components(
        errors, state.get("blind_since"), now, ts)
    tripped = tripped + blind_hits
    # Alert once per (metric, day); re-alert intraday only if the value got
    # WORSE. The inbox path already dedups via event_id, but history got a
    # record on EVERY 2h pass while a rule stayed tripped — and metrics-digest
    # is forbidden from merging records, so a persisting condition became a
    # fresh 🚨 card every cycle (20+ "一手源挂了" cards in the week of 7/13,
    # 10 on 7/19 alone, for a condition the evening investigation concluded
    # was upstream jitter). Level-triggered pages are for machines; the owner
    # gets edge-triggered ones.
    raw_alerted = state.get("alerted_today")
    alerted = dict(raw_alerted) if isinstance(raw_alerted, dict) else {}
    if alerted.get("date") != today or not isinstance(
            alerted.get("metrics"), dict):
        # collect() must never raise (adapter contract) — malformed persisted
        # state resets rather than KeyError-ing the source into a permanent
        # crash (red-team 7/20 finding #6).
        alerted = {"date": today, "metrics": {}}
    # Compare against the state as of loop START: two rules on one metric
    # (warning >=10, critical >=100) must BOTH alert on the pass that trips
    # them — comparing against a mid-loop write silently dropped the
    # escalation tier (red-team 7/20 finding #5).
    prev_alerted = dict(alerted["metrics"])
    fresh_hits = []
    for hit in tripped:
        prev_actual = prev_alerted.get(hit["metric"])
        if prev_actual is not None:
            worse = (hit["actual"] > prev_actual if hit["op"] in (">", ">=")
                     else hit["actual"] < prev_actual if hit["op"] in ("<", "<=")
                     else False)
            if not worse:
                continue
        alerted["metrics"][hit["metric"]] = hit["actual"]
        fresh_hits.append(hit)
    new_state_alerted = alerted
    for hit in fresh_hits:
        if hit.get("error") is not None:
            title = (f"🚨 {name} 失明: {hit['component']} 读不到，第 "
                     f"{_fmt(hit['actual'])} 天 — {hit['error']}")
            body = (f"{hit['component']} 自 {hit['since']} 起读不到: {hit['error']}\n"
                    f"这段时间里靠它的指标全是空的，不是正常。")
        else:
            title = (f"🚨 {name} 异常: {hit['metric']} {hit['op']} "
                     f"{hit['threshold_desc']} (当前 {_fmt(hit['actual'])})")
            body = (f"规则: {hit['metric']} {hit['op']} {hit['threshold_desc']}\n"
                    f"当前值: {_fmt(hit['actual'])}")
        signals.append({
            "event_id": f"anomaly:{hit['metric']}:{today}",
            "ts": ts,
            "title": title,
            "summary": title,
            "body": body,
            "actor": {"raw": "", "resolved": ""},
            "payload": {"kind": "anomaly", "metric": hit["metric"],
                        "actual": hit["actual"]},
        })
        record = {
            "ts": ts, "date": today, "kind": "anomaly", "name": name,
            "metric": hit["metric"], "rule": hit["rule"], "actual": hit["actual"],
            "threshold": hit["threshold"], "digest_hint": digest_hint,
        }
        if hit.get("error") is not None:
            record.update({"component": hit["component"], "error": hit["error"],
                           "since": hit["since"]})
        _append_history(history, record)
    new_state["alerted_today"] = new_state_alerted
    for component, since in blind_recovered:
        # Sight restored: say so once, right away (the digest files ✅ as a
        # notice), and clear the alert marker so a relapse alerts again.
        metric = f"blind:{component}"
        alerted["metrics"].pop(metric, None)
        title = f"✅ {name} 恢复: {component} 又读到了 (自 {since} 起失明)"
        signals.append({
            "event_id": f"recovery:{metric}:{today}",
            "ts": ts,
            "title": title,
            "summary": title,
            "body": f"{component} 自 {since} 起读不到，本次探测已恢复。",
            "actor": {"raw": "", "resolved": ""},
            "payload": {"kind": "recovery", "metric": metric, "actual": 0},
        })
        _append_history(history, {
            "ts": ts, "date": today, "kind": "recovery", "name": name,
            "metric": metric, "actual": 0, "tripped_on": since[:10],
            "component": component, "digest_hint": digest_hint,
        })

    snapshot_due = (now.hour >= snapshot_hour
                    and state.get("last_snapshot_date") != today)

    # Recovery: a metric that alarmed on a PREVIOUS day and whose rules were
    # evaluated clean this pass gets one ✅ all-clear, checked only at
    # snapshot time (once daily — no intraday flapping).
    tripped_dates = dict(state.get("tripped") or {})
    for hit in tripped:
        tripped_dates[hit["metric"]] = today
    if snapshot_due:
        for m in sorted(evaluated_clean):
            d = tripped_dates.get(m)
            if d and d < today:
                title = f"✅ {name} 恢复: {m} 回到正常 (当前 {_fmt(metrics[m])})"
                signals.append({
                    "event_id": f"recovery:{m}:{today}",
                    "ts": ts,
                    "title": title,
                    "summary": title,
                    "body": f"{m} 曾于 {d} 触发告警，本次评估已回到正常范围。",
                    "actor": {"raw": "", "resolved": ""},
                    "payload": {"kind": "recovery", "metric": m,
                                "actual": metrics[m]},
                })
                _append_history(history, {
                    "ts": ts, "date": today, "kind": "recovery", "name": name,
                    "metric": m, "actual": metrics[m], "tripped_on": d,
                    "digest_hint": digest_hint,
                })
                del tripped_dates[m]
    # GC: drop trip markers older than a week (rule removed / never re-clean)
    from datetime import timedelta
    stale_cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    new_state["tripped"] = {m: d for m, d in tripped_dates.items()
                            if d >= stale_cutoff}

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
        if not (_is_number(rule.get("value")) or _is_number(rule.get("pct_of_prev"))
                or _is_number(rule.get("pct_of_baseline"))):
            errs.append(f"rules[{i}]: needs numeric value or pct_of_prev "
                        "or pct_of_baseline")
    hour = cfg.get("snapshot_hour")
    if hour is not None and (not _is_number(hour) or not 0 <= hour <= 23):
        errs.append("snapshot_hour: must be 0-23")
    return errs
