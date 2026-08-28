"""Component manifest checks — "what should be running" as data (REQ-40).

The dashboard died for 23 days because four supervision surfaces each kept a
hardcoded, divergent component list. components.yaml is now the single source
of truth and this module the single checker, consumed by:

  - tasks/self_diagnostic_pre.sh   (every 4h, full list → ⚠️ lines → REQ-39 alert)
  - daemon.py                      (every 30s, critical subset, alert-only)
  - scripts/doctor.sh              (install-time)
  - restart.sh --status            (operator view)

CLI:
    python3 -m core.components            # full report, ⚠️ lines on failure
    python3 -m core.components --critical # critical subset only
    python3 -m core.components --json     # machine-readable
Exit code: number of failing components (0 = all green), capped at 100.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANIFEST = ROOT / "components.yaml"

# Per-root config cache — check_components can be called every 30s by the
# daemon; re-parsing jarvis.yaml per component would be wasteful.
_config_cache: dict[str, object] = {}


def _config_get(root: Path, dotpath: str):
    """Read a dotted key from <root>/jarvis.yaml. Missing/broken config →
    None (treated as 'not configured'). Never raises."""
    key = str(root)
    if key not in _config_cache:
        try:
            from core.config import Config
            _config_cache[key] = Config(root / "jarvis.yaml")
        except Exception:
            _config_cache[key] = None
    cfg = _config_cache[key]
    if cfg is None:
        return None
    try:
        return cfg.get(dotpath)
    except Exception:
        return None


# A red verdict from a check that VERIFIED the process is alive. The daemon
# renders every manifest-critical red as 「组件失联：X 没有在运行」, which on
# 2026-08-18 02:16 was simply false: ef-stream's process ran the whole time,
# it was the health file that had gone stale behind a sleeping host. An alert
# that names the wrong failure sends the reader to restart something that is
# already running (feedback: 老板面的卡不能喊错狼).
ALIVE_BUT_SILENT = "进程在跑但没在报状态"


def _post_wake_grace(root: Path, now: float | None = None) -> str | None:
    """The daemon's persisted post-wake hold, or None when it is not active.

    Only the daemon watches the host sleep/wake gap, so it alone decides how
    long "everything looks stale" is explained by a closed lid. Every age- or
    overdue-derived verdict in this module reads that one window instead of
    inventing its own — otherwise each check re-decides the same question and
    they disagree.

    2026-08-18/19: the host slept ~39h across 38 gaps. `heartbeat-tasks` was
    the only check reading this window, so it correctly held green while
    `ef-stream` — same host, same wake, process alive the whole time — went
    critical on "protocol health stale" and pagedthe owner with "EigenFlux 实时
    消息接收没有在运行". The one alert that reached him in 39 hours named the
    wrong thing, because a check that could not see the sleep blamed the only
    thing it could see.

    Grace covers staleness only. Liveness (pid/pgrep/http) is never excused:
    a process that died during sleep is dead when we wake, and the window is
    bounded (30min), so a component that is genuinely stuck still turns red
    shortly after the host is back.
    """
    now = time.time() if now is None else now
    try:
        brain = json.loads(
            (root / ".daemon_brain_state.json").read_text(encoding="utf-8"))
        grace_until = float(brain.get("grace_until", 0) or 0)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if now >= grace_until:
        return None
    return f"post-wake grace — settles in {int((grace_until - now) / 60)}min"


def _awake_age(root: Path, since_epoch: float,
               now: float | None = None) -> tuple[float, float]:
    """``(age counting only awake time, host sleep discounted)`` in seconds.

    The bounded post-wake grace above is an *excuse* with a timer on it, and
    its own author flagged the hole: on a laptop that naps hourly the window
    re-arms nearly continuously (23 wakes in 24h here), so a component that is
    genuinely wedged can hide behind a hold that never lapses. Subtracting the
    sleep the daemon actually recorded (`core.hostclock`) answers the real
    question instead — "have we been UP this long without hearing from it?" —
    and cannot be renewed by another nap.

    Grace remains the fallback only where there is NO recorded sleep in the
    window (a fresh install, or the first wake after this ships). Once an
    episode exists, awake-age is the answer and the timer does not get to
    override it — that is exactly the renewing-hold hole.
    """
    from core import hostclock

    moment = time.time() if now is None else now
    return (hostclock.awake_age(root, since_epoch, now=moment),
            hostclock.slept_between(root, since_epoch, moment))


def _slept_note(slept_s: float) -> str:
    return (f"; {slept_s / 3600:.1f}h host sleep not counted"
            if slept_s >= 3600 else "")


def _gate_reason(comp: dict, root: Path) -> str | None:
    """Fresh-install/optional-feature gate (2026-07-13): components.yaml used
    to mark ef-stream/lark-sidecar/admin critical UNCONDITIONALLY while
    doctor.sh calls the same features optional — a collaborator's default
    install (admin.enabled: false, no eigenflux CLI, no launchd services)
    alarmed [critical] forever on components that were never supposed to run.
    A component whose declared precondition is unmet is SKIPPED (reported ok,
    marked skipped), not red. Preconditions:
      requires_cmd:    <binary>            — command must exist on PATH
      requires_file:   <path>              — file must exist (~ expanded;
                                             relative paths resolve from root)
      requires_config: <dot.path>[=value]  — jarvis.yaml key truthy / == value
    Returns the human-readable skip reason, or None when armed."""
    cmd = comp.get("requires_cmd")
    if cmd and shutil.which(str(cmd)) is None:
        return f"{cmd} not installed"
    fpath = comp.get("requires_file")
    if fpath:
        p = Path(os.path.expanduser(str(fpath)))
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            return f"{fpath} not installed"
    ckey = comp.get("requires_config")
    if ckey:
        ckey = str(ckey)
        expected = None
        if "=" in ckey:
            ckey, expected = ckey.split("=", 1)
        val = _config_get(root, ckey)
        if expected is None:
            if not val:
                return f"{ckey} not enabled in jarvis.yaml"
        elif str(val) != expected:
            return f"{ckey} != {expected} in jarvis.yaml"
    return None


def load_manifest(path: Path | None = None) -> list[dict]:
    import yaml
    p = path or MANIFEST
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except (OSError, Exception):
        return []
    return list(data.get("components", []))


def _check_pid(comp: dict, root: Path) -> tuple[bool, str]:
    pid_file = root / comp.get("path", "")
    try:
        first = pid_file.read_text().strip().split()[0]
        pid = int(first)
    except (OSError, ValueError, IndexError):
        return False, "pidfile missing/unreadable"
    try:
        os.kill(pid, 0)
        return True, f"pid {pid} alive"
    except (ProcessLookupError, PermissionError):
        return False, f"pid {pid} dead"


def _read_pidfile(root: Path, path: str) -> int | None:
    try:
        return int((root / path).read_text().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _has_ancestor(pid: int, ancestor: int, procs: dict[int, tuple[int, str]]) -> bool:
    seen = set()
    cur = pid
    while cur and cur not in seen:
        if cur == ancestor:
            return True
        seen.add(cur)
        cur = procs.get(cur, (0, ""))[0]
    return False


def _check_pgrep(comp: dict, root: Path) -> tuple[bool, str]:
    pattern = comp.get("pattern", "")
    owner = _read_pidfile(root, comp.get("owned_by_pidfile", "")) \
        if comp.get("owned_by_pidfile") else None
    try:
        # macOS pgrep -f only inspects a truncated cmdline (~first 100 chars),
        # so a long interpreter path prefix can hide patterns like
        # "-m core.heartbeat_loop". Use a full-cmdline ps scan as the source
        # of truth; it also lets us filter out this diagnostic command and
        # orphaned processes not owned by bot.sh.
        ps = subprocess.run(["ps", "ax", "-o", "pid=,command="],
                            capture_output=True, text=True, timeout=5)
        ps_tree = subprocess.run(["ps", "ax", "-o", "pid=,ppid=,command="],
                                 capture_output=True, text=True, timeout=5)
        procs: dict[int, tuple[int, str]] = {}
        for line in ps_tree.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            try:
                procs[int(parts[0])] = (int(parts[1]), parts[2])
            except ValueError:
                continue
        pids = []
        for line in ps.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            pid, _, cmd = line.partition(" ")
            try:
                pid_i = int(pid)
            except ValueError:
                continue
            if not pattern or pattern not in cmd:
                continue
            if (
                "core.components" in cmd
                or "rg " in cmd
                or " --system-prompt " in cmd
                or "/claude" in cmd
                or cmd.startswith("claude ")
            ):
                continue
            if owner and not _has_ancestor(pid_i, owner, procs):
                continue
            pids.append(str(pid_i))
        if pids:
            detail = f"pids {pids}"
            if owner:
                detail += f" owned by {owner}"
            return True, detail
        if owner:
            # Wording matters: "no process owned by pid X" read as if two
            # components shared one PID (X is the BOT's pid, the same owner
            # for every child — 2026-07-13 confusion). Say what it means.
            return False, f"not running (bot pid {owner} has no matching child process)"
        return False, "no process"
    except Exception as e:
        return False, f"pgrep error: {e}"


def _check_http(comp: dict, root: Path) -> tuple[bool, str]:
    url = comp.get("url", "")
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = resp.status < 500
            return ok, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # The server responded — it's reachable, not down. A 5xx (e.g. /health
        # returning 503 "degraded" because a circuit is open) is a soft failure;
        # do NOT report it as "unreachable", which falsely reads as a dead process.
        if e.code >= 500:
            return False, f"degraded (HTTP {e.code})"
        return True, f"HTTP {e.code}"
    except Exception as e:
        return False, f"unreachable ({type(e).__name__})"


def _check_file_age(comp: dict, root: Path) -> tuple[bool, str]:
    import time
    p = root / comp.get("path", "")
    max_h = float(comp.get("max_age_hours", 24))
    try:
        age_h = (time.time() - p.stat().st_mtime) / 3600
    except OSError:
        return False, "file missing"
    if age_h > max_h:
        awake_s, slept_s = _awake_age(root, p.stat().st_mtime)
        if awake_s / 3600 <= max_h:
            return True, (f"age {awake_s / 3600:.1f}h (max {max_h:.0f}h)"
                          f"{_slept_note(slept_s)}")
        grace = _post_wake_grace(root) if slept_s <= 0 else None
        if grace:
            return True, f"{grace} (age {age_h:.1f}h)"
    return (age_h <= max_h,
            f"age {age_h:.1f}h (max {max_h:.0f}h)")


def _ef_reconcile_health(
    comp: dict,
    root: Path,
    *,
    now: float | None = None,
) -> tuple[bool, str, float]:
    """Return whether the polling ingress safety net recently completed.

    Real-time streaming and polling are redundant ingress paths. Keeping this
    read in one helper prevents the component verdict from accidentally
    treating the safety net as a prerequisite for an otherwise healthy stream.
    """
    moment = time.time() if now is None else float(now)
    reconcile_path = root / comp.get(
        "reconcile_path", "data/ef_ingress_health.json"
    )
    try:
        reconcile = json.loads(reconcile_path.read_text(encoding="utf-8"))
        success = float(reconcile.get("last_success_epoch") or 0)
        status = str(reconcile.get("status") or "unknown")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        success = 0
        status = "unavailable"
    age = max(0.0, moment - success) if success > 0 else float("inf")
    max_age = float(comp.get("reconcile_max_age_seconds", 900))
    return success > 0 and age <= max_age and status == "ok", status, age


def _check_ef_stream(comp: dict, root: Path) -> tuple[bool, str]:
    """Check end-to-end EigenFlux ingress, not one preferred transport.

    The WebSocket stream is the low-latency path; ``eigenflux-inbox-reconcile``
    is the durable polling safety net. Either recently verified path preserves
    delivery. A network/VPN wobble must not make Guardian kill a live stream
    merely because the independent poll was stale (2026-08-26 incident).
    """
    now = time.time()
    reconcile_ok, reconcile_status, reconcile_age = _ef_reconcile_health(
        comp, root, now=now
    )
    process_ok, process_detail = _check_pgrep(comp, root)
    if not process_ok:
        if reconcile_ok:
            return True, (
                "real-time stream unavailable; polling fallback verified "
                f"{int(reconcile_age)}s ago; {process_detail}"
            )
        return False, (
            f"{process_detail}; polling safety net "
            f"{reconcile_status}/stale"
        )
    path = root / comp.get("path", "data/ef_stream_health.json")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        updated = float(state.get("updated_epoch") or 0)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        if reconcile_ok:
            return True, (
                "real-time health unavailable; polling fallback verified "
                f"{int(reconcile_age)}s ago; {process_detail}"
            )
        return False, (f"{ALIVE_BUT_SILENT}；{process_detail}; "
                       f"protocol health unavailable")
    age = now - updated
    if updated <= 0 or age > float(comp.get("max_age_seconds", 2400)):
        # The stream writes its health file from inside this host. A host that
        # was asleep produces exactly this reading with nothing wrong.
        awake_s, slept_s = _awake_age(root, updated) if updated > 0 else (age, 0)
        if updated > 0 and awake_s <= float(comp.get("max_age_seconds", 2400)):
            return True, (f"quiet {awake_s / 60:.0f}min while awake"
                          f"{_slept_note(slept_s)}; {process_detail}")
        grace = _post_wake_grace(root) if slept_s <= 0 else None
        if grace and updated > 0:
            # Carry the real age: on a laptop that naps hourly the window can
            # re-arm often, and a hold that keeps renewing over a genuinely
            # wedged stream must still be visible to whoever reads the report.
            return True, (f"{grace}; health {age / 3600:.1f}h old; "
                          f"{process_detail}")
        if reconcile_ok:
            return True, (
                f"real-time health stale; polling fallback verified "
                f"{int(reconcile_age)}s ago; {process_detail}"
            )
        return False, f"{ALIVE_BUT_SILENT}；{process_detail}; protocol health stale"
    status = str(state.get("status") or "unknown")
    quiet = int(state.get("quiet_streak") or 0)
    started = float(state.get("started_epoch") or updated)
    grace = float(comp.get("connect_grace_seconds", 600))
    if status == "active":
        poll = (
            f"poll verified {int(reconcile_age)}s ago"
            if reconcile_ok else f"poll {reconcile_status}/stale"
        )
        return True, (
            f"active; {poll}; quiet streak {quiet}; {process_detail}"
        )
    if status in {"connecting", "reconnecting", "degraded"}:
        if reconcile_ok:
            return True, (
                f"{status}; poll verified {int(reconcile_age)}s ago; "
                f"quiet streak {quiet}; {process_detail}"
            )
        if status != "degraded" and now - started <= grace:
            return True, (
                f"{status}; startup grace, poll {reconcile_status}; "
                f"{process_detail}"
            )
        return False, (
            f"{ALIVE_BUT_SILENT}；{status}; polling safety net "
            f"{reconcile_status}/stale; "
            f"{process_detail}"
        )
    if reconcile_ok:
        return True, (
            f"real-time {status}; polling fallback verified "
            f"{int(reconcile_age)}s ago; {process_detail}"
        )
    detail = str(state.get("detail") or status)
    return False, (f"{ALIVE_BUT_SILENT}；{status}: {detail}; "
                   f"{process_detail}")[:400]


def _check_deadman(comp: dict, root: Path) -> tuple[bool, str]:
    from core.deadman import status

    result = status(root)
    return result.status == "ok", result.detail


def _check_audit_age(comp: dict, root: Path) -> tuple[bool, str]:
    p = root / comp.get("path", "")
    max_h = float(comp.get("max_age_hours", 24))
    if not p.is_file():
        return False, "database missing"
    try:
        from core.conversation_audit import connect
        db = connect(p)
        row = db.execute(
            "SELECT completed_at FROM audit_runs "
            "WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        return False, f"audit read failed ({type(exc).__name__})"
    finally:
        if "db" in locals():
            db.close()
    if row is None:
        return False, "no completed audit run"
    try:
        completed = datetime.fromisoformat(str(row[0]))
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        age_h = (
            datetime.now(timezone.utc) - completed.astimezone(timezone.utc)
        ).total_seconds() / 3600
    except (TypeError, ValueError, OverflowError):
        return False, "invalid completed audit timestamp"
    if not 0 <= age_h <= max_h:
        completed_epoch = completed.astimezone(timezone.utc).timestamp()
        awake_s, slept_s = _awake_age(root, completed_epoch)
        if 0 <= awake_s / 3600 <= max_h:
            return True, (f"completed age {awake_s / 3600:.1f}h "
                          f"(max {max_h:.0f}h){_slept_note(slept_s)}")
        grace = _post_wake_grace(root) if slept_s <= 0 else None
        if grace and age_h >= 0:
            return True, f"{grace} (completed age {age_h:.1f}h)"
    return (
        0 <= age_h <= max_h,
        f"completed age {age_h:.1f}h (max {max_h:.0f}h)",
    )


def _check_heartbeat_tasks(comp: dict, root: Path) -> tuple[bool, str]:
    """Task-level heartbeat health — a live process is not a working scheduler.

    On 2026-07-27 between 05:58 and 09:59 the heartbeat process was alive, so
    the `pgrep` check on heartbeat-loop reported healthy and this manifest read
    15/15 green — while activity-log failed every run, intention-check had been
    wedged 11h, and memory-tidy / self-diagnostic had gone 16h / 15h without a
    success. The daemon's brain-health path did page correctly; the manifest,
    which PRODUCT.md counts on for "silent component outage duration", did not
    see it at all.

    This is deliberately not a second detector: it calls the same pure
    `brain_health.assess` the daemon pages on, with the same inputs, and honors
    the same persisted post-wake grace so a laptop that just woke does not read
    red here while the daemon deliberately holds. It never writes brain state —
    the daemon remains the only owner of that ledger and the only pager.
    """
    import time
    try:
        from core import brain_health
        from core.heartbeat import HeartbeatRunner, parse_heartbeat
        from core.task_protocol import CircuitState
    except Exception as exc:  # a partial install must not crash the report
        return False, f"brain-health unavailable ({type(exc).__name__})"

    def _read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text()) or {}
        except (OSError, ValueError):
            return {}

    state = _read_json(root / comp.get("path", "heartbeat_state.json"))
    if not state:
        return False, "heartbeat state unreadable"
    tasks = parse_heartbeat(root / "HEARTBEAT.md")
    if not tasks:
        return False, "HEARTBEAT.md unreadable"
    from core.interval_config import parse_interval_overrides
    overrides = parse_interval_overrides(
        _read_json(root / "interval_overrides.json"))
    brain = _read_json(root / ".daemon_brain_state.json")

    now = time.time()
    grace = _post_wake_grace(root, now)
    if grace:
        return True, grace

    result = brain_health.assess(
        state=state, tasks=tasks, overrides=overrides,
        priority_tasks=HeartbeatRunner.PRIORITY_TASKS,
        prev_samples=brain.get("samples", {}) or {},
        now=now,
        failure_threshold=CircuitState.FAILURE_THRESHOLD,
    )
    alerts = list(result.get("alerts") or [])
    if not result.get("brain_dead"):
        detail = f"{len(tasks)} tasks, none stalled"
        if alerts:
            # Sub-threshold starvation: real, but not yet systemic. Say so
            # rather than printing a bare "none stalled" over the top of it.
            detail = f"{len(tasks)} tasks, {len(alerts)} lagging (below alert threshold)"
        return True, detail
    shown = "；".join(alerts[:3])
    more = f"（还有 {len(alerts) - 3} 个）" if len(alerts) > 3 else ""
    return False, f"{len(alerts)} 个心跳任务停摆：{shown}{more}"[:400]


def _check_delivery(comp: dict, root: Path) -> tuple[bool, str]:
    """Detect a live runtime whose user-facing delivery has stopped working."""
    path = root / str(comp.get("path") or "data/jarvis.db")
    now = time.time()
    window = max(60, int(comp.get("window_seconds", 3600) or 3600))
    failure_limit = max(1, int(comp.get("failure_streak", 3) or 3))
    max_overdue = max(
        60, int(comp.get("max_overdue_seconds", 900) or 900)
    )
    db = None
    try:
        db = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=1,
        )
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT state,attempts FROM delivery_envelopes "
            "WHERE created_epoch>=? AND state IN "
            "('queued','attempting','failed','delivered','read','acted') "
            "ORDER BY created_epoch DESC LIMIT 100",
            (now - window,),
        ).fetchall()
        overdue = db.execute(
            "SELECT COUNT(*) AS count,"
            "MIN(COALESCE(next_attempt_epoch,created_epoch)) AS oldest_due "
            "FROM delivery_envelopes WHERE state IN ('queued','attempting') "
            "AND attempts>0 AND COALESCE(next_attempt_epoch,0)<=?",
            (now,),
        ).fetchone()
    except (OSError, sqlite3.Error, ValueError) as exc:
        return False, f"delivery ledger unreadable ({type(exc).__name__})"
    finally:
        if db is not None:
            db.close()

    failure_streak = 0
    for row in rows:
        state = str(row["state"] or "")
        if state in {"delivered", "read", "acted"}:
            break
        if state == "failed" or (
            state in {"queued", "attempting"}
            and int(row["attempts"] or 0) >= 3
        ):
            failure_streak += 1

    overdue_count = int(overdue["count"] or 0) if overdue else 0
    oldest_due = float(overdue["oldest_due"] or now) if overdue else now
    overdue_age = max(0, int(now - oldest_due))
    if failure_streak >= failure_limit:
        return False, (
            f"delivery unavailable: {failure_streak} consecutive failed "
            f"envelope(s) in {window // 60}min"
        )
    if overdue_count and overdue_age > max_overdue:
        # Everything queued goes overdue while the lid is shut; the failure
        # streak above is the signal that survives sleep, this one does not.
        awake_s, slept_s = _awake_age(root, oldest_due, now)
        if awake_s <= max_overdue:
            return True, (
                f"delivery catching up: {overdue_count} due item(s), oldest "
                f"{int(awake_s) // 60}min awake{_slept_note(slept_s)}"
            )
        grace = _post_wake_grace(root, now) if slept_s <= 0 else None
        if grace:
            return True, (
                f"{grace} ({overdue_count} due item(s), oldest "
                f"{overdue_age // 60}min)"
            )
        return False, (
            f"delivery stalled: {overdue_count} due item(s), oldest "
            f"{overdue_age // 60}min"
        )
    if not rows:
        return True, "delivery quiet; no recent failure streak or due queue"
    return True, (
        f"delivery healthy; failure streak {failure_streak}, "
        f"due queue {overdue_count}"
    )


def _check_model_runtime(comp: dict, root: Path) -> tuple[bool, str]:
    """Inspect model execution receipts without starting or restarting models."""
    path = root / str(comp.get("path") or "data/jarvis.db")
    now = time.time()
    window = max(60, int(comp.get("window_seconds", 3600) or 3600))
    failure_limit = max(1, int(comp.get("failure_streak", 3) or 3))
    stale_after = max(60, int(comp.get("stale_after_seconds", 1800) or 1800))
    integrity_window = max(
        window,
        int(comp.get("integrity_window_seconds", 86400) or 86400),
    )
    db = None
    try:
        db = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=1,
        )
        db.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"model_runtime_calls", "model_runtime_attempts"}
        if not required.issubset(tables):
            return False, "model runtime receipt schema is not initialized"
        stale = db.execute(
            "SELECT COUNT(*) AS count,MIN(started_epoch) AS oldest "
            "FROM model_runtime_calls WHERE status='running' "
            "AND started_epoch<?",
            (now - stale_after,),
        ).fetchone()
        mismatched = db.execute(
            """SELECT COUNT(*) AS count FROM model_runtime_calls c
                 WHERE c.started_epoch>=? AND c.status!='running'
                   AND c.attempt_count!=(
                       SELECT COUNT(*) FROM model_runtime_attempts a
                        WHERE a.call_id=c.id
                   )""",
            (now - integrity_window,),
        ).fetchone()
        ambiguous = db.execute(
            """SELECT COUNT(*) AS count FROM model_runtime_calls
                 WHERE started_epoch>=? AND status='ambiguous'
                   AND effect_authority IN ('workspace_write','external')""",
            (now - integrity_window,),
        ).fetchone()
        rows = db.execute(
            """SELECT status FROM model_runtime_calls
                 WHERE started_epoch>=? AND status!='running'
                 ORDER BY started_epoch DESC LIMIT 100""",
            (now - window,),
        ).fetchall()
    except (OSError, sqlite3.Error, ValueError) as exc:
        return False, f"model runtime receipts unreadable ({type(exc).__name__})"
    finally:
        if db is not None:
            db.close()

    stale_count = int(stale["count"] or 0) if stale else 0
    if stale_count:
        oldest = float(stale["oldest"] or now)
        return False, (
            f"model runtime stalled: {stale_count} call(s), oldest "
            f"{max(0, int(now - oldest)) // 60}min"
        )
    mismatch_count = int(mismatched["count"] or 0) if mismatched else 0
    if mismatch_count:
        return False, f"model runtime receipt mismatch: {mismatch_count} call(s)"
    ambiguous_count = int(ambiguous["count"] or 0) if ambiguous else 0
    if ambiguous_count:
        return False, (
            f"model runtime has {ambiguous_count} recent ambiguous "
            "write/external call(s)"
        )

    failure_streak = 0
    for row in rows:
        status = str(row["status"] or "")
        if status == "succeeded":
            break
        if status in {"failed", "ambiguous"}:
            failure_streak += 1
        elif status == "cancelled":
            break
    if failure_streak >= failure_limit:
        return False, (
            f"model runtime unavailable: {failure_streak} consecutive failed "
            f"call(s) in {window // 60}min"
        )
    if not rows:
        return True, "model runtime quiet; no recent calls or stale receipts"
    return True, f"model runtime healthy; failure streak {failure_streak}"


def _check_launchctl(comp: dict, root: Path) -> tuple[bool, str]:
    label = comp.get("label", "")
    try:
        uid = os.getuid()
        r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                           capture_output=True, text=True, timeout=5)
        return (r.returncode == 0,
                "loaded" if r.returncode == 0 else "not loaded")
    except Exception as e:
        return False, f"launchctl error: {e}"


def _check_taskline(comp: dict, root: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["taskline", "status"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return False, f"status probe failed ({type(exc).__name__})"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "probe failed").strip()
        return False, detail[:240]
    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, "status probe returned invalid JSON"
    healthy = bool(payload.get("healthy") and payload.get("registered"))
    return healthy, (
        "healthy and workspace registered"
        if healthy
        else "server unhealthy or workspace unregistered"
    )


def _check_runtime_source(comp: dict, root: Path) -> tuple[bool, str]:
    """Verify that the bot watchdog is allowed to respawn its children.

    The bot deliberately freezes its source revision for its lifetime. A
    checkout change or a runtime-path edit therefore disarms child respawns.
    Process probes can all remain green in that state, so expose the exact
    guard as a component instead of pretending the stack is fully healthy.
    """
    pid_path = root / str(comp.get("pid_path", ".bot.pid"))
    paths_path = root / str(comp.get("paths_file", "runtime_sources.txt"))
    try:
        fields = pid_path.read_text(encoding="utf-8").strip().split()
    except OSError:
        return False, "bot runtime receipt missing"
    if len(fields) < 3 or not fields[2]:
        return False, "bot runtime revision missing; governed restart required"
    boot_head = fields[2]

    try:
        paths = [
            line.strip()
            for line in paths_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return False, "runtime source manifest missing"
    if not paths:
        return False, "runtime source manifest empty"

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        if head.returncode != 0:
            return False, "current runtime revision unavailable"
        current_head = head.stdout.strip()
        if current_head != boot_head:
            return False, (
                "bot loaded a different revision; governed restart required "
                f"(boot {boot_head[:12]}, checkout {current_head[:12]})"
            )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        if dirty.returncode != 0:
            return False, "runtime source status unavailable"
    except Exception as exc:
        return False, f"runtime source probe failed ({type(exc).__name__})"
    changed = [line.rstrip() for line in dirty.stdout.splitlines() if line.strip()]
    if changed:
        sample = ", ".join(line[3:] for line in changed[:3])
        suffix = f" (+{len(changed) - 3} more)" if len(changed) > 3 else ""
        return False, f"runtime source modified: {sample}{suffix}"
    return True, f"watchdog armed at {boot_head[:12]}"


_CHECKS = {
    "pid": _check_pid,
    "pgrep": _check_pgrep,
    "http": _check_http,
    "file_age": _check_file_age,
    "ef_stream": _check_ef_stream,
    "deadman": _check_deadman,
    "audit_age": _check_audit_age,
    "heartbeat_tasks": _check_heartbeat_tasks,
    "delivery": _check_delivery,
    "model_runtime": _check_model_runtime,
    "launchctl": _check_launchctl,
    "taskline": _check_taskline,
    "runtime_source": _check_runtime_source,
}


def check_components(critical_only: bool = False,
                     manifest_path: Path | None = None,
                     root: Path | None = None) -> list[dict]:
    """Run all manifest checks. Returns [{name, ok, detail, critical}, ...]."""
    root = root or ROOT
    results = []
    for comp in load_manifest(manifest_path):
        if critical_only and not comp.get("critical", False):
            continue
        reason = _gate_reason(comp, root)
        if reason:
            # ok=True keeps every consumer (daemon critical probe, ⚠️ line
            # extraction, exit code) treating an unconfigured optional
            # feature as healthy; skipped=True lets reports label it.
            results.append({"name": comp.get("name", "?"), "ok": True,
                            "skipped": True,
                            "detail": f"skipped — {reason}",
                            "critical": bool(comp.get("critical", False))})
            continue
        fn = _CHECKS.get(comp.get("check", ""))
        if fn is None:
            results.append({"name": comp.get("name", "?"), "ok": False,
                            "detail": f"unknown check type {comp.get('check')!r}",
                            "critical": bool(comp.get("critical", False))})
            continue
        try:
            ok, detail = fn(comp, root)
        except Exception as e:  # a checker bug must not kill the report
            ok, detail = False, f"checker crashed: {e}"
        degraded = bool(
            ok
            and comp.get("check") == "ef_stream"
            and (
                str(detail).startswith((
                    "degraded;",
                    "connecting;",
                    "reconnecting;",
                    "real-time ",
                ))
                and ("poll verified" in str(detail)
                     or "polling fallback verified" in str(detail))
            )
        )
        results.append({"name": comp.get("name", "?"), "ok": ok,
                        "detail": detail,
                        "degraded": degraded,
                        "critical": bool(comp.get("critical", False))})
    return results


def format_report(results: list[dict]) -> str:
    """Human/diagnostic report — failures as ⚠️ lines (REQ-39 picks them up)."""
    lines = ["--- Components ---"]
    for r in results:
        mark = ("○" if r.get("skipped") else
                ("△" if r.get("degraded") else ("✓" if r["ok"] else "⚠️")))
        crit = " [critical]" if r["critical"] and not r["ok"] else ""
        lines.append(f"  {mark} {r['name']}: {r['detail']}{crit}")
    bad = sum(1 for r in results if not r["ok"])
    skipped = sum(1 for r in results if r.get("skipped"))
    degraded = sum(1 for r in results if r.get("degraded"))
    tail = f"  ({len(results) - bad}/{len(results)} healthy"
    if skipped:
        tail += f", {skipped} skipped — optional features not configured"
    if degraded:
        tail += f", {degraded} degraded — fallback still serving"
    lines.append(tail + ")")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    critical = "--critical" in argv
    results = check_components(critical_only=critical)
    if "--json" in argv:
        print(json.dumps(results, ensure_ascii=False))
    else:
        print(format_report(results))
    return min(100, sum(1 for r in results if not r["ok"]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
