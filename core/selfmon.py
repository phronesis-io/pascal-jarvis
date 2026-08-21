"""Self-monitoring computed from LIVE sources (REQ-67).

Why this module exists — the audit finding "self-monitoring tables frozen at
2026-05-21":

PRD v2 established that the shared SQLite tables must not be the monitor's
source of truth. (History: the 2026-05-21 audit found them frozen. Since
then core/delivery.py writes a 'sent' attribution row to engagement_events
on every delivered envelope — the table is live again but one-sided, and
agent_log stays unwritten; every response fact and scheduling fact exists
only in the files below.) The source of truth is the LIVE append-only files
the running bot actually maintains:

  - sched_events.jsonl       every scheduling decision + intent_* lifecycle
  - engagement_log.jsonl     every proactive 'sent' card + user response
  - heartbeat_outbox.jsonl   real deliveries
  - data/jarvis.db           the intent DB (closure_status lives here)
  - jarvis.log               structured JSONL prose (failure signatures)

So self-monitoring is REBUILT here as pure functions that read those live
sources read-only and compute the metrics, rather than reviving the dead
tables. This is Pascal's data-first principle applied to the monitor itself:
the metric you watch must come from the same bytes the system actually writes.

Everything is read-only. The intent DB is opened sqlite `mode=ro` so a buggy
monitor can never mutate production state. No function here raises on a missing
or malformed file — a broken monitor must degrade to zeros/'n/a', never take
down a caller (the CLI or the REQ-39 self-diagnostic).

Public surface:
  compute_selfmon(jarvis_dir, window_hours=24) -> dict
  selfmon_report(jarvis_dir, window_hours=24) -> str   (⚠️ lines for REQ-39)
  selfmon_liveness_ok(jarvis_dir) -> (bool, str)       (startup assertion)

CLI:
  python3 -m core.selfmon [--dir DIR] [--window-hours N] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ── tunables ──────────────────────────────────────────────────────────────

# A 'sent' source whose engagement (engaged + late_reply responses) over the
# window is at or below this fraction is flagged as low-value noise — it is
# costing Pascal proactive cards without earning replies.
LOW_ENGAGEMENT_THRESHOLD = 0.15

# same_intent_refires: an intent that actually starts this many attempts or
# more inside REFIRE_WINDOW_MIN is the "triple-nag" class. A retry marker
# followed by intent_fired is one attempt, not two events.
REFIRE_WINDOW_MIN = 30
REFIRE_MIN_COUNT = 3

# closure_overdue: fallback TTL for an unknown category. Normal production
# decisions use the same per-category CLOSURE_POLICY as the lifecycle sweeper.
CLOSURE_OVERDUE_DAYS = 3

# task_finish statuses that mean the cycle ran but the work did not succeed.
CRASH_STATUSES = {"failed", "parse_failed", "timeout"}

# Failure fingerprints that can hide inside a log message even when the
# surrounding event does not carry a useful structured status.
LOG_FAILURE_SIGNATURES = (
    "timed out (60s)",
    "JSON parse failed",
    "exited with code 1",
    "stderr:",
    # REQ-94: memory tier truncation. core/memory.py emits this structured
    # warn "so selfmon/alerting can see it" — but until 2026-07-14 nothing
    # consumed it, and the system tier silently dropped ~24k chars/cycle
    # (all of inbox_private_mail) for days. warm-tier squeeze (by design)
    # rides the same event with expected=true — filtered in silent_failures,
    # not here.
    "tier_truncated",
)

# Bound the jarvis.log scan — only the tail can be recent; reading a multi-MB
# rotated log fully on every refresh would be wasteful.
LOG_TAIL_BYTES = 512 * 1024


# ── time helpers ───────────────────────────────────────────────────────────

def _parse_epoch(ts) -> float | None:
    """Best-effort local-time string → epoch.

    Handles the two shapes that appear across the live files:
      sched_events / engagement : 'YYYY-MM-DD HH:MM[:SS]'
      jarvis.log                : 'YYYY-MM-DDTHH:MM:SS'  (ISO, no tz)
    """
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except (ValueError, TypeError):
            continue
    # ISO with timezone offset / fractional seconds — let datetime try.
    try:
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _read_jsonl(path: Path, max_bytes: int | None = None) -> list[dict]:
    """Read a JSONL file read-only into a list of dicts. Never raises.

    If max_bytes is given, only the trailing window is read (the first,
    possibly partial, line of that window is dropped). Bad lines are skipped
    individually so one corrupt row can't lose the rest.
    """
    out: list[dict] = []
    try:
        if max_bytes is not None:
            size = path.stat().st_size
            with open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                    f.readline()  # drop the partial leading line
                raw = f.read().decode("utf-8", errors="ignore")
        else:
            raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


# Bound the per-refresh parse (red-team fix): the retired dashboard panel
# called compute_selfmon every 10s and sched_events grows to a 10MB rotation cap, so an
# unbounded full re-parse (twice per call) blocked the asyncio loop ~75ms→~1s.
# selfmon metrics are 24h-windowed; a 2MB tail covers far more than 24h of
# events. Memoize on (size, mtime) so the two callers in one compute_selfmon
# parse at most once, and a 10s refresh on an unchanged file is free.
SCHED_TAIL_BYTES = 2 * 1024 * 1024
_sched_cache: dict = {"key": None, "events": []}


def _read_sched_events(jarvis_dir: Path) -> list[dict]:
    """sched_events.jsonl tail (bounded), oldest first. Cached on (size, mtime)."""
    base = jarvis_dir / "sched_events.jsonl"
    try:
        st = base.stat()
        key = (str(base), st.st_size, st.st_mtime)
    except OSError:
        key = (str(base), 0, 0)
    if _sched_cache["key"] == key:
        return _sched_cache["events"]
    events = _read_jsonl(base, max_bytes=SCHED_TAIL_BYTES)
    _sched_cache["key"] = key
    _sched_cache["events"] = events
    return events


# ── DB access (read-only) ──────────────────────────────────────────────────

def _intent_db_path(jarvis_dir: Path) -> Path:
    """Location of the intent DB. Kept as a separate hook so tests can seed a
    tmp DB without touching the real file."""
    return jarvis_dir / "data" / "jarvis.db"


def _open_intent_db_ro(jarvis_dir: Path) -> sqlite3.Connection | None:
    """Open the intent DB strictly read-only (sqlite URI mode=ro). Returns None
    if the file is missing or unopenable — a monitor must never create or
    mutate the production DB."""
    db_path = _intent_db_path(jarvis_dir)
    if not db_path.exists():
        return None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


# ── metric 1: noise card count ─────────────────────────────────────────────

def noise_card_count(jarvis_dir: Path, since_epoch: float,
                     window_hours: float) -> dict:
    """Proactive cards sent in the window, by source, with low-engagement flags.

    From engagement_log.jsonl: count 'sent' rows per source, and pair each
    source with its engagement rate (responses reacting engaged/late_reply vs
    sent). A high-volume / low-engagement source is the 'noise' class.
    """
    rows = _read_jsonl(jarvis_dir / "engagement_log.jsonl")
    sent: Counter = Counter()
    replied: Counter = Counter()
    for e in rows:
        ep = e.get("epoch")
        ep = float(ep) if isinstance(ep, (int, float)) else _parse_epoch(e.get("ts"))
        if ep is None or ep < since_epoch:
            continue
        src = str(e.get("source", "") or "unknown")
        t = e.get("type")
        if t == "sent":
            sent[src] += 1
        elif t == "response" and e.get("reaction") in ("engaged", "late_reply"):
            replied[src] += 1

    days = max(window_hours / 24.0, 1e-9)
    by_source: dict[str, dict] = {}
    low_engagement: list[str] = []
    for src, n in sent.most_common():
        rate = round(replied.get(src, 0) / n, 3) if n else 0.0
        by_source[src] = {"sent": n, "per_day": round(n / days, 2),
                          "engaged_replies": replied.get(src, 0),
                          "engagement_rate": rate}
        # Only flag sources that actually cost something (>=2 cards) — a single
        # unanswered card is not yet a noise pattern.
        if n >= 2 and rate <= LOW_ENGAGEMENT_THRESHOLD:
            low_engagement.append(src)

    total = sum(sent.values())
    return {
        "total_sent": total,
        "per_day": round(total / days, 2),
        "by_source": by_source,
        "low_engagement_sources": low_engagement,
    }


# ── metric 2: same-intent re-fires (triple-nag class) ──────────────────────

def same_intent_refires(jarvis_dir: Path, since_epoch: float) -> dict:
    """Intents that started REFIRE_MIN_COUNT+ attempts within the window.

    A normal retry is logged twice: first as ``intent_retry`` when reconciliation
    schedules it, then as ``intent_fired`` when the next attempt starts. Counting
    both made one successful self-heal look like a triple fire. Prefer actual
    ``intent_fired`` rows; retain retry-only rows as a compatibility fallback for
    older or partial logs. Uses a sliding window over per-intent timestamps.
    """
    window_s = REFIRE_WINDOW_MIN * 60
    fired: dict[str, list[tuple[float, str]]] = defaultdict(list)
    retries: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for e in _read_sched_events(jarvis_dir):
        ev = str(e.get("event", ""))
        if ev not in ("intent_fired", "intent_retry"):
            continue
        ep = _parse_epoch(e.get("ts"))
        if ep is None or ep < since_epoch:
            continue
        iid = str(e.get("task", "") or e.get("name", "") or "?")
        target = fired if ev == "intent_fired" else retries
        target[iid].append((ep, str(e.get("name", "") or "")))

    offenders: dict[str, dict] = {}
    for iid in fired.keys() | retries.keys():
        events = fired.get(iid) or retries[iid]
        events.sort(key=lambda x: x[0])
        times = [t for t, _ in events]
        # Max number of events inside any REFIRE_WINDOW_MIN sliding window.
        best = 0
        lo = 0
        for hi in range(len(times)):
            while times[hi] - times[lo] > window_s:
                lo += 1
            best = max(best, hi - lo + 1)
        if best >= REFIRE_MIN_COUNT:
            name = next((nm for _, nm in events if nm), "")
            offenders[iid] = {"name": name, "total": len(events),
                              "max_in_window": best,
                              "window_min": REFIRE_WINDOW_MIN}

    return {
        "offender_count": len(offenders),
        "offenders": offenders,
    }


# ── metric 3: closure overdue (zombie awaiting intents) ────────────────────

def closure_overdue(jarvis_dir: Path, now: float,
                    overdue_days: int | None = None) -> dict:
    """Awaiting closures past policy TTL with no live follow-up.

    Reads the intent DB read-only. The anchor age is taken from the most
    specific timestamp available (closed_at is terminal so it's excluded):
    executed_at → triggered_at → created_at, mirroring intentions._sweep.
    ``overdue_days`` is a test/diagnostic override; production uses the
    authoritative per-category lifecycle policy.
    """
    from core.intentions import CLOSURE_POLICY

    conn = _open_intent_db_ro(jarvis_dir)
    if conn is None:
        return {"overdue_count": 0, "overdue": [], "db_available": False}

    overdue: list[dict] = []
    try:
        try:
            rows = conn.execute(
                "SELECT i.id, i.name, i.category, i.created_at, "
                "i.triggered_at, i.executed_at, i.closure_followup_id, "
                "fu.status AS followup_status "
                "FROM intentions i LEFT JOIN intentions fu "
                "ON fu.id = i.closure_followup_id "
                "WHERE i.closure_status = 'awaiting'"
            ).fetchall()
        except sqlite3.Error:
            return {"overdue_count": 0, "overdue": [], "db_available": False}
        for r in rows:
            d = dict(r)
            if d.get("followup_status") in ("pending", "triggered"):
                continue
            category = str(d.get("category") or "none")
            policy = CLOSURE_POLICY.get(category, CLOSURE_POLICY["none"])
            ttl_days = int(
                overdue_days
                if overdue_days is not None
                else policy.get("awaiting_ttl_days", CLOSURE_OVERDUE_DAYS)
            )
            cutoff = now - ttl_days * 86400
            anchor = d.get("executed_at") or d.get("triggered_at") or d.get("created_at")
            ep = _parse_epoch(anchor)
            # Anchors are ISO ('2026-06-13T09:00:00') or space-separated; both
            # handled by _parse_epoch. An unparseable anchor on an awaiting row
            # is itself suspect, so treat it as overdue rather than hiding it.
            if ep is None or ep <= cutoff:
                age_days = round((now - ep) / 86400, 1) if ep is not None else None
                overdue.append({"id": d.get("id"), "name": d.get("name", ""),
                                "anchor": anchor, "age_days": age_days,
                                "category": category, "ttl_days": ttl_days})
    finally:
        conn.close()

    return {"overdue_count": len(overdue), "overdue": overdue,
            "db_available": True, "overdue_days": overdue_days,
            "policy": "override" if overdue_days is not None else "category"}


# ── metric 4: model crash / skip ───────────────────────────────────────────

def model_crash_or_skip(jarvis_dir: Path, since_epoch: float,
                        window_hours: float) -> dict:
    """task_finish in (failed, parse_failed, timeout) + circuit_tripped events,
    counted per task. Also surfaces the per-day rate."""
    crashes: Counter = Counter()
    by_status: Counter = Counter()
    circuit_trips: Counter = Counter()
    for e in _read_sched_events(jarvis_dir):
        ep = _parse_epoch(e.get("ts"))
        if ep is None or ep < since_epoch:
            continue
        ev = str(e.get("event", ""))
        task = str(e.get("task", "") or "")
        if ev in ("task_finish", "task_timeout"):
            status = str(e.get("status", "")) or ("timeout" if ev == "task_timeout" else "")
            if ev == "task_timeout":
                status = "timeout"
            if status in CRASH_STATUSES:
                crashes[task] += 1
                by_status[status] += 1
        elif ev == "circuit_tripped":
            circuit_trips[task] += 1

    days = max(window_hours / 24.0, 1e-9)
    total = sum(crashes.values()) + sum(circuit_trips.values())
    return {
        "total": total,
        "per_day": round(total / days, 2),
        "by_status": dict(by_status),
        "by_task": dict(crashes),
        "circuit_trips": dict(circuit_trips),
    }


# ── metric 5: silent failures (info-level fingerprints in the log) ──────────

def silent_failures(jarvis_dir: Path, since_epoch: float) -> dict:
    """Count LOG_FAILURE_SIGNATURES occurrences in the jarvis.log tail.

    These are the failures that hide at info level — nothing trips a circuit,
    nothing alerts, but a fingerprint string ('timed out (60s)', 'JSON parse
    failed', …) shows up in the log msg. Counted per signature.
    """
    rows = _read_jsonl(jarvis_dir / "jarvis.log", max_bytes=LOG_TAIL_BYTES)
    by_signature: Counter = Counter()
    samples: dict[str, str] = {}
    for e in rows:
        ep = _parse_epoch(e.get("ts"))
        if ep is None or ep < since_epoch:
            continue
        msg = str(e.get("msg", ""))
        # Entries the emitter marked expected=true are BY-DESIGN events that
        # merely look like failures (warm-tier squeeze, elected primary probe
        # while the spend-limit gate is tripped). Counting them re-creates the
        # false-outage reading REQ-94/96 were written to stop.
        if e.get("expected") is True:
            continue
        level = str(e.get("level", "")).lower()
        for sig in LOG_FAILURE_SIGNATURES:
            # heartbeat records stderr from successful helper scripts at info
            # level. Those lines are diagnostic transcripts, not failures.
            if sig == "stderr:" and level == "info":
                continue
            if sig in msg:
                by_signature[sig] += 1
                if sig not in samples:
                    samples[sig] = msg[:160]
    return {
        "total": sum(by_signature.values()),
        "by_signature": dict(by_signature),
        "samples": samples,
    }


# ── aggregate ──────────────────────────────────────────────────────────────

def compute_selfmon(jarvis_dir, window_hours: float = 24) -> dict:
    """Compute all self-monitoring metrics over the window from LIVE sources.

    Read-only. Never raises — any per-metric read failure degrades that metric
    to its empty shape (so the CLI and self-diagnostic always render).
    """
    jd = Path(jarvis_dir)
    now = time.time()
    since = now - window_hours * 3600

    def _safe(fn, empty):
        try:
            return fn()
        except Exception:  # noqa: BLE001 — a broken metric must not break the rest
            return empty

    noise = _safe(lambda: noise_card_count(jd, since, window_hours),
                  {"total_sent": 0, "per_day": 0.0, "by_source": {},
                   "low_engagement_sources": []})
    refires = _safe(lambda: same_intent_refires(jd, since),
                    {"offender_count": 0, "offenders": {}})
    overdue = _safe(lambda: closure_overdue(jd, now),
                    {"overdue_count": 0, "overdue": [], "db_available": False})
    crashes = _safe(lambda: model_crash_or_skip(jd, since, window_hours),
                    {"total": 0, "per_day": 0.0, "by_status": {},
                     "by_task": {}, "circuit_trips": {}})
    silent = _safe(lambda: silent_failures(jd, since),
                   {"total": 0, "by_signature": {}, "samples": {}})

    return {
        "window_hours": window_hours,
        "computed_at": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "noise_card_count": noise,
        "same_intent_refires": refires,
        "closure_overdue": overdue,
        "model_crash_or_skip": crashes,
        "silent_failures": silent,
    }


def selfmon_report(jarvis_dir, window_hours: float = 24) -> str:
    """Human / diagnostic report. Anomalies are emitted as `  ⚠️ …` lines so the
    REQ-39 self-diagnostic alert path (which greps ⚠️ out of section text) picks
    them up automatically. A clean window emits a single ✓ line."""
    m = compute_selfmon(jarvis_dir, window_hours)
    wh = m["window_hours"]
    lines = [f"--- Self-monitoring ({_fmt_window(wh)}) ---"]
    warns: list[str] = []

    noise = m["noise_card_count"]
    lines.append(f"  cards sent: {noise['total_sent']} "
                 f"({noise['per_day']}/day)")
    for src in noise["low_engagement_sources"]:
        info = noise["by_source"].get(src, {})
        warns.append(f"  ⚠️ low-engagement source '{src}': "
                     f"{info.get('sent', 0)} cards, "
                     f"{int(info.get('engagement_rate', 0) * 100)}% replied")

    refires = m["same_intent_refires"]
    if refires["offender_count"]:
        for iid, info in refires["offenders"].items():
            label = info.get("name") or iid
            warns.append(f"  ⚠️ intent re-fire storm '{label}' ({iid}): "
                         f"{info['max_in_window']} fires in "
                         f"{info['window_min']}min (triple-nag class)")
    else:
        lines.append("  same-intent re-fires: none")

    overdue = m["closure_overdue"]
    if not overdue.get("db_available", False):
        lines.append("  closure-overdue: n/a (intent DB unavailable)")
    elif overdue["overdue_count"]:
        for it in overdue["overdue"]:
            age = it.get("age_days")
            age_str = f"{age}d" if age is not None else "unknown-age"
            ttl_days = it.get("ttl_days", overdue.get("overdue_days"))
            warns.append(f"  ⚠️ closure zombie '{it.get('name') or it.get('id')}'"
                         f" awaiting {age_str} (>{ttl_days}d)")
    else:
        lines.append("  closure-overdue: 0")

    crashes = m["model_crash_or_skip"]
    if crashes["total"]:
        detail = ", ".join(f"{k}={v}" for k, v in crashes["by_status"].items())
        trips = sum(crashes["circuit_trips"].values())
        warns.append(f"  ⚠️ task crashes/skips: {crashes['total']} "
                     f"({crashes['per_day']}/day"
                     + (f", {detail}" if detail else "")
                     + (f", circuit_trips={trips}" if trips else "") + ")")
    else:
        lines.append("  task crashes/skips: 0")

    silent = m["silent_failures"]
    if silent["total"]:
        detail = ", ".join(f"{k!r}×{v}" for k, v in silent["by_signature"].items())
        warns.append(f"  ⚠️ silent failures in log: {silent['total']} ({detail})")
    else:
        lines.append("  silent failures: 0")

    if warns:
        lines.extend(warns)
    else:
        lines.append("  ✓ no self-monitoring anomalies in window")
    return "\n".join(lines)


def _fmt_window(window_hours: float) -> str:
    if window_hours % 24 == 0 and window_hours >= 24:
        d = int(window_hours / 24)
        return f"{d}d" if d > 1 else "24h"
    return f"{int(window_hours)}h"


# ── liveness assertion (startup) ───────────────────────────────────────────

def selfmon_liveness_ok(jarvis_dir) -> tuple[bool, str]:
    """Startup assertion: is the self-monitoring data actually flowing?

    The failure this closes: if sched_events.jsonl has gone silent (the
    scheduler event log is the second-fresh heartbeat-of-the-monitor), every
    metric above silently reads zero and looks healthy — exactly the
    'tables frozen since 2026-05-21' class, just relocated to the live file.

    So: if there are ZERO sched_events in the last 24h WHILE there is
    independent evidence the system is alive (active session transcripts exist,
    or heartbeat_state shows a run in the last 24h), the monitor is dead.
    Returns (False, reason). If both the event log AND the corroborating
    liveness signals are quiet, we cannot distinguish 'monitor dead' from
    'genuinely idle box' → (True, ...) (don't cry wolf on a powered-off laptop).
    """
    jd = Path(jarvis_dir)
    now = time.time()
    cutoff = now - 24 * 3600

    recent_events = 0
    for e in _read_sched_events(jd):
        ep = _parse_epoch(e.get("ts"))
        if ep is not None and ep >= cutoff:
            recent_events += 1
            if recent_events >= 1:
                break

    if recent_events > 0:
        return True, f"ok: {recent_events}+ sched_events in last 24h"

    # No recent events — is the system otherwise alive?
    alive_evidence = []

    # active_sessions.json is a DURABLE conv→session map that is never pruned
    # (red-team fix): once Pascal has ever messaged the bot it is permanently
    # non-empty, so "non-empty" is NOT a liveness signal — it false-fired
    # 'self-monitoring dead' on an idle/closed laptop. Require RECENCY: the
    # file (or heartbeat_state) must have been touched within the window.
    sessions = jd / "active_sessions.json"
    try:
        if sessions.exists() and sessions.stat().st_mtime >= cutoff:
            alive_evidence.append("active_sessions updated in 24h")
    except OSError:
        pass

    try:
        state = json.loads((jd / "heartbeat_state.json").read_text(encoding="utf-8"))
        if isinstance(state, dict):
            for ts in state.values():
                if isinstance(ts, dict) and (ts.get("last_run", 0) or 0) >= cutoff:
                    alive_evidence.append("heartbeat_state shows a run in 24h")
                    break
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    if alive_evidence:
        return False, ("self-monitoring dead: no sched_events in 24h "
                       f"(but {'; '.join(alive_evidence)})")
    return True, "idle: no sched_events and no corroborating liveness signals"


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Self-monitoring metrics computed from LIVE sources (REQ-67).")
    ap.add_argument("--dir", default=os.environ.get("JARVIS_DIR", "."),
                    help="jarvis dir holding the live files (default: $JARVIS_DIR or .)")
    ap.add_argument("--window-hours", type=float, default=24,
                    help="lookback window in hours (default: 24)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable JSON instead of the text report")
    args = ap.parse_args(argv)

    if args.json:
        out = compute_selfmon(args.dir, args.window_hours)
        ok, reason = selfmon_liveness_ok(args.dir)
        out["liveness_ok"] = ok
        out["liveness_reason"] = reason
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(selfmon_report(args.dir, args.window_hours))
        ok, reason = selfmon_liveness_ok(args.dir)
        print(f"\nliveness: {'OK' if ok else 'DEAD'} — {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
