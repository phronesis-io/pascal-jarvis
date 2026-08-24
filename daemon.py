#!/usr/bin/env python3
"""Jarvis Guardian Daemon — keeps the system alive and healthy.

A persistent background process that:
1. Periodically checks Jarvis health (bot.sh, heartbeat, Lark listener)
2. On failure: notifies Pascal via Lark + auto-restarts
3. Stays alive forever — designed to be managed by launchd (KeepAlive)

This is the ONE process that must never die. It's intentionally minimal
and defensive — no complex dependencies, no imports from core/.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Inline local-time helper (daemon avoids core/ imports for resilience)
def _daemon_now() -> datetime:
    """Current time in system-local timezone, immune to TZ env corruption."""
    try:
        link = Path("/etc/localtime").resolve()
        parts = link.parts
        for i, p in enumerate(parts):
            if p == "zoneinfo" and i + 1 < len(parts):
                tz_name = "/".join(parts[i + 1:])
                from zoneinfo import ZoneInfo
                return datetime.now(ZoneInfo(tz_name))
    except Exception:
        pass
    return datetime.now()

# ── Config ──
JARVIS_DIR = Path(__file__).parent
CHECK_INTERVAL = 30           # seconds between health checks
HEARTBEAT_STALE_THRESHOLD = 1800  # 30 min without heartbeat = stale (Claude calls take 30-90s, cycles ~20min apart)
WAKE_GRACE_SECONDS = 180      # after host sleep/wake, suppress stale-heartbeat restarts briefly
SLEEP_GAP_THRESHOLD = 120     # daemon loop slept > expected by this much ⇒ host sleep/pause
# Brain-death alerting needs a much longer post-wake grace than the restart
# path: after hours of laptop sleep (or a forced-macOS-update reboot) EVERY
# task's last_success is stale by the nap length, and the first post-wake
# retries routinely fail (network/VPN come up after we do). That is not
# brain-death. 30min covers a full heartbeat cycle plus its ≤5min fast-retries.
BRAIN_WAKE_GRACE = 30 * 60
# _check_brain_health runs every ~4min; a hole > this in our own check cadence
# means the host slept or was shut down. This catches reboots, where the
# in-memory last_wake_time mark is wiped but BRAIN_STATE_FILE survives.
BRAIN_CHECK_GAP_THRESHOLD = 15 * 60
# Grace penetration (audit 7/10): BRAIN_WAKE_GRACE was re-armed on EVERY wake
# and check-gap with no memory of what it kept suppressing, so on a lid-open-
# 10-40min commute day (7/10: 09:20/09:54/10:37/11:07) a wedge that persisted
# 17.5h never produced a single page — 'would alert but in post-wake grace'
# forever. The grace exists to filter the stale-looking first minutes AFTER a
# wake; a failure still present in a SECOND awake window (it survived a full
# sleep without healing), or suppressed for a cumulative hour, is precisely
# NOT that artefact and must page through.
BRAIN_SUPPRESS_MAX_WINDOWS = 2
BRAIN_SUPPRESS_MAX_SECONDS = 3600
# 2s TCP probe to tell "system broken" from "host offline" before a
# brain-death page (7/9 flight day: awake >30min with no network made
# calendar-sync et al look wedged and a BRAIN-DEAD page fired — the system
# was merely offline, and the page could only land in the dead-letter file
# to be re-delivered stale after landing). Same host notify_lark needs, so
# unreachable ⇒ the page couldn't be delivered now anyway.
REACHABILITY_PROBE = ("open.feishu.cn", 443)
REACHABILITY_TIMEOUT = 2.0
MAX_RESTART_ATTEMPTS = 3
RESTART_COOLDOWN = 300        # 5 min between restart attempts
# Overridable so pytest can point log writes at tmp_path: on 7/7 a deadletter
# test ran log() unstubbed and wrote fake WARN/ERROR rows into the real
# daemon.log (fixture stub added in 072cf2f — this is the belt-and-braces for
# future tests that forget it).
LOG_FILE = Path(os.environ.get("JARVIS_DAEMON_LOG") or JARVIS_DIR / "daemon.log")
DAEMON_PID_FILE = JARVIS_DIR / ".daemon.pid"
BOT_PID_FILE = JARVIS_DIR / ".bot.pid"
# macOS banner fallback (see _raise_banner). Persisted so hot-reload respawns
# cannot reset the rate limit — the same reason PROBE_ALERT_STATE_FILE exists.
BANNER_STAMP_FILE = JARVIS_DIR / "data" / ".daemon_banner_stamp"
BANNER_MIN_INTERVAL = 3600
# Restart budget persisted across daemon hot-reloads (REQ-42 respawns): the
# in-memory counters reset on every reincarnation, so a crash-looping stack
# used to get MAX_RESTART_ATTEMPTS fresh attempts per respawn, defeating the
# breaker. Same load/save robustness as BRAIN_STATE_FILE.
RESTART_STATE_FILE = JARVIS_DIR / ".daemon_restart_state.json"
# Breaker latch (7/7 audit): the max-attempts branch used to zero its own
# counter with only a 10min cooldown, so a permanently broken stack got
# 3 restart storms + 1 page per ~35min forever. While this flag file exists
# ALL auto-restarts are held; deleting it (manual re-arm) or an observed
# healthy check clears it.
BREAKER_LATCH_FILE = JARVIS_DIR / "data" / "restart_breaker.latched"
MAX_LOG_LINES = 1000
# Touched by core/heartbeat_loop._beat() on every call (including throttled
# ones) — log-format-independent liveness after two log-parsing regressions
# in a row (10KB tail; beats[-1] concatenation order, audit 7/8). Absent until
# the loop redeploys, so the log grep in _find_last_heartbeat stays.
HEARTBEAT_BEAT_STAMP = JARVIS_DIR / "data" / ".heartbeat_beat"
# A restart whose 90s post-check missed a slow warm-up leaks its increment
# into RESTART_STATE_FILE forever — three unrelated incidents weeks apart then
# latch the breaker with zero actual restart attempts (audit 7/8). A sustained
# healthy stretch returns the budget; hours, not minutes, so a stack flapping
# faster than this still accumulates to the latch it deserves.
RESTART_BUDGET_RESET_WINDOW = 6 * 3600
# Self-diagnostic watch (stability backlog #5): the 4h task is the system's
# only aggregated health alarm, and nothing watched the watcher. The stamp is
# SHARED with tasks/self_diagnostic_post.py (same {"ts", "lines"} shape) so
# both delivery paths honour one dedup window.
DIAG_PRE_FILE = JARVIS_DIR / ".diag_last_pre.txt"
DIAG_ALERT_STAMP = JARVIS_DIR / ".diag_last_alert.json"
DIAG_ALERT_WINDOW = 4 * 3600      # = self_diagnostic_post.DEDUP_WINDOW_S
# Two missed runs + margin; coupled to self-diagnostic's 4h interval in
# HEARTBEAT.md — tune together.
DIAG_STALE_THRESHOLD = 9 * 3600
# heartbeat_state.json's last_run carries the cycle-START time (run_cycle
# captures `now` before pre-scripts run), while .diag_last_pre.txt is written
# mid-cycle after up to ~10min of sequential 60s-bounded pre-scripts — so
# even a COMPLETED cycle shows last_run slightly behind the pre mtime. Leg B's
# "the post had its turn" comparison allows this much skew. An in-flight
# cycle's last_run (the previous run, ~4h back) never comes close.
DIAG_CYCLE_START_SKEW = 15 * 60
# Manifest-critical probes run every ~2min (probe_tick%4 × 30s); bot.sh's own
# watchdog respawns a dead ef-stream within ≤30s. Page only when a component
# stays red across TWO consecutive probes at least this far apart — a
# watchdog-healed transient never pages, a real outage pages within ~4min.
MANIFEST_CONFIRM_WINDOW = 2 * 60
# A repair request is asynchronous: bot.sh's watchdog checks children every
# 30s and launchd may need a few seconds to recreate a UI process.  Give the
# repaired component enough time to complete a real heartbeat cycle before a
# user-facing alert.  The timestamp is persisted in the existing probe/brain
# state files, so daemon hot reloads cannot accidentally skip the wait.
COMPONENT_RECOVERY_GRACE = 2 * 60
BRAIN_RECOVERY_GRACE = 20 * 60
DIAG_RECOVERY_GRACE = 20 * 60

# Lark config (read from jarvis.yaml)
USER_ID = ""
try:
    import yaml
    cfg = yaml.safe_load((JARVIS_DIR / "jarvis.yaml").read_text())
    USER_ID = cfg.get("lark", {}).get("user_id", "")
except Exception:
    try:
        for line in (JARVIS_DIR / "jarvis.yaml").read_text().splitlines():
            if "user_id" in line:
                USER_ID = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
    except Exception:
        pass

# ── State ──
last_restart_time = 0
restart_count = 0
running = True
last_wake_time = 0.0
# Process start. Hot-reload respawns reset it by design: a fresh daemon can't
# tell a reboot from a redeploy, so both get the same post-start alert grace
# (reboot wipes last_wake_time; see _check_diag_staleness).
_daemon_started = time.time()


def _load_restart_state():
    """Restore the persisted restart budget. Corrupt/missing file → defaults."""
    global restart_count, last_restart_time
    try:
        state = json.loads(RESTART_STATE_FILE.read_text())
        restart_count = int(state.get("restart_count", 0))
        last_restart_time = float(state.get("last_restart_time", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        restart_count = 0
        last_restart_time = 0


def _save_restart_state():
    """Persist the restart budget on every mutation. Never raises."""
    try:
        tmp = RESTART_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"restart_count": restart_count,
                                   "last_restart_time": last_restart_time}))
        os.replace(tmp, RESTART_STATE_FILE)
    except OSError:
        pass


def log(level: str, msg: str):
    """Append to daemon.log with rotation."""
    ts = _daemon_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
        if LOG_FILE.stat().st_size > 200_000:
            lines = LOG_FILE.read_text().splitlines()
            trimmed = "\n".join(lines[-500:]) + "\n"
            tmp = LOG_FILE.with_suffix(".tmp")
            tmp.write_text(trimmed)
            os.replace(tmp, LOG_FILE)
    except Exception:
        pass


# How the delivery state machine can dispose of a Guardian alert. Only the
# LOST set means "Pascal will never see this" — everything else either already
# reached him, is still in flight, or is a deliberate de-duplication of an
# alert he has. Treating them all as failure is what produced 322 ERROR lines
# and 322 macOS banners titled "Lark链路不通" over two days in which the Lark
# link was fine (2026-08-16/17): the dead-letter consumer only marks its rows
# notified when notify_lark returns True, so every "duplicate" verdict left
# the rows pending and re-alerted them ~2min later, forever.
_ALERT_REACHED_STATES = {"delivered", "read", "acted"}
_ALERT_IN_FLIGHT_STATES = {"queued", "attempting"}
# A metric cap is reached only when another active envelope with the same
# incident throttle key already exists. It therefore means "covered by the
# existing incident", not "this new row was delivered".
_ALERT_COVERED_REASONS = {"metric_daily_cap"}


def _alert_disposition(result) -> tuple[str, str]:
    """(disposition, one-line explanation) for a Guardian delivery result.

    ``confirmed`` and ``covered`` may close the source incident. ``pending``
    means the durable envelope is still responsible for delivery, so it must
    neither close a dead-letter row nor raise a local failure banner. ``lost``
    is the only state that escalates out of band.
    """
    state = str(getattr(result, "state", "") or "")
    reason = str(getattr(result, "reason", "") or "")
    if state in _ALERT_REACHED_STATES:
        return "confirmed", "delivered"
    if not getattr(result, "accepted", False):
        return "lost", f"delivery refused the alert (state={state}, {reason})"
    if state in _ALERT_IN_FLIGHT_STATES:
        return "pending", f"queued for delivery ({reason or 'retrying'})"
    if state == "suppressed" and reason in _ALERT_COVERED_REASONS:
        return "covered", f"same incident already active ({reason})"
    return "lost", f"alert dropped (state={state}, reason={reason})"


def _raise_banner(msg: str, why: str) -> None:
    """Local macOS notification — the daemon's only out-of-band channel.

    Rate-limited and persisted: a banner storm is indistinguishable from
    wallpaper, and the whole point of this channel is that it still means
    something when the Lark link is down. The text is the alert itself in
    plain Chinese; the reason goes to the log, not to the notification centre.
    """
    now = time.time()
    try:
        last = float(BANNER_STAMP_FILE.read_text().strip())
    except (OSError, ValueError):
        last = 0.0
    if now - last < BANNER_MIN_INTERVAL:
        log("INFO", f"Guardian banner suppressed (last one "
            f"{int(now - last)}s ago, limit 1/{BANNER_MIN_INTERVAL}s): {why}")
        return
    try:
        body = " ".join(str(msg).split())[:180]
        result = subprocess.run(
            ["osascript", "-e",
             'on run argv\n'
             'display notification (item 1 of argv) with title '
             '"Jarvis：这条没能发到飞书"\n'
             'end run', body],
            capture_output=True, timeout=5)
        if result.returncode != 0:
            log("WARN", f"Guardian banner failed: osascript exit "
                f"{result.returncode}")
            return
    except Exception:
        return
    try:
        BANNER_STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BANNER_STAMP_FILE.with_suffix(".tmp")
        tmp.write_text(str(now))
        os.replace(tmp, BANNER_STAMP_FILE)
    except OSError:
        pass


def notify_lark(msg: str, incident_key: str = "") -> bool | None:
    """Submit a Guardian alert to the unified delivery state machine.

    The daemon remains model-independent and keeps the macOS banner as its
    truly out-of-band fallback. Retry, dedup, throttle, and queueing are no
    longer reimplemented here.

    Returns True only when the alert was confirmed or an existing incident
    already covers it, None while a durable envelope remains in flight, and
    False only when it was genuinely lost. This three-way receipt prevents a
    queued warning from being called delivered while also preventing a local
    banner storm for work that is still retrying.
    """
    if not USER_ID:
        log("WARN", "No USER_ID configured, cannot notify Lark")
        _raise_banner(msg, "no user_id configured")
        return False
    try:
        import hashlib
        from core.delivery import DeliveryEnvelope, deliver

        stable = re.sub(r"[^a-zA-Z0-9:_-]+", "-", str(incident_key).strip())
        metric = stable[:80] if stable else hashlib.sha256(
            " ".join(str(msg).split()).encode("utf-8")).hexdigest()[:20]
        result = deliver(
            DeliveryEnvelope(
                source="guardian-daemon",
                kind="text",
                payload={"text": f"🛡️ **Guardian Daemon**\n\n{msg}"},
                attention="alert",
                requested_channel="lark",
                urgent=True,
                dedup_key=f"guardian:{metric}",
                throttle_key=f"guardian:{metric}",
                metadata={
                    "metric_daily_cap": 1,
                    "incident_key": metric,
                    "audience": "owner_private",
                    "recipient_type": "open_id",
                    "replayable": False,
                    # A dead letter about this dead letter would recurse
                    # forever during a Lark outage.
                    "suppress_dead_letter": True,
                },
            ),
            root=JARVIS_DIR,
        )
        disposition, why = _alert_disposition(result)
        if disposition in {"confirmed", "covered"}:
            if disposition == "covered":
                log("INFO", f"Guardian incident already covered: {why}")
            return True
        if disposition == "pending":
            log("INFO", f"Guardian alert remains in flight: {why}")
            return None
        log("ERROR", f"Guardian alert lost: {why}")
    except Exception as e:
        log("ERROR", f"Lark notify failed: {e}")
        _raise_banner(msg, f"notify raised {type(e).__name__}")
        return False

    _raise_banner(msg, why)
    return False


def _find_last_heartbeat() -> float | None:
    """Find the most recent heartbeat liveness signal — beat stamp file or
    'Beat sent' log line. Returns age in seconds, or None if not found."""
    # Check both log locations
    log_files = [
        Path("/tmp/jarvis_restart.log"),
        JARVIS_DIR / "jarvis.log",
    ]
    latest_beat = None

    for log_path in log_files:
        if not log_path.exists():
            continue
        try:
            # Read the last 256KB (jarvis.log rotates at ~500KB so cost is
            # bounded). Red-team 7/8: the old 10KB tail was sized for the
            # per-10s beat spam — with beats throttled to BEAT_LOG_INTERVAL_S
            # any busy stretch (conversation handler lines, ef-stream, outage
            # warnings) pushed the newest beat out of the window → false
            # "No heartbeat found" → full-stack restart mid-conversation.
            size = log_path.stat().st_size
            with open(log_path, "r", errors="ignore") as f:
                if size > 262_144:
                    f.seek(size - 262_144)
                    f.readline()  # skip partial line
                text = f.read()
            # Match both old format [YYYY-MM-DD HH:MM:SS]...Beat sent
            # and new JSON format {"ts": "YYYY-MM-DDTHH:MM:SS",...}
            beats = re.findall(
                r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*heartbeat.*Beat sent",
                text
            )
            # Also check structured JSON logs. \s* after the colons: core/
            # log.py emits '"ts": "' WITH a space — the old tight regex
            # matched zero real lines (red-team 7/8). Any heartbeat-component
            # JSON line proves the loop is alive, so no 'Beat sent' required.
            json_beats = re.findall(
                r'"ts":\s*"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})".*"component":\s*"heartbeat"',
                text
            )
            for tb in json_beats:
                beats.append(tb.replace("T", " "))
            if beats:
                # max(), not beats[-1]: JSON matches are appended AFTER all
                # bracket matches, so list order is not file order — with a
                # quiet-but-alive loop the last JSON line can predate a fresh
                # 'Beat sent' by >25min, overstating beat_age toward a false
                # restart (audit 7/8, armed by ea62974's regex fix). Both
                # formats are fixed-width local time, so lexicographic max is
                # chronological.
                beat_time = datetime.strptime(max(beats), "%Y-%m-%d %H:%M:%S")
                if latest_beat is None or beat_time > latest_beat:
                    latest_beat = beat_time
        except Exception:
            continue

    ages = []
    if latest_beat:
        ages.append((_daemon_now().replace(tzinfo=None) - latest_beat).total_seconds())
    # Beat stamp file: freshest source wins. Tolerated absent — the loop code
    # touching it may not be deployed yet, and log parsing still covers that.
    try:
        ages.append(time.time() - HEARTBEAT_BEAT_STAMP.stat().st_mtime)
    except OSError:
        pass
    return min(ages) if ages else None


def _record_wake_gap(slept_for_s: float, expected_s: float = CHECK_INTERVAL) -> float:
    """Record a recent host wake when the daemon's own sleep overshot.

    The daemon loop sleeps in 1s chunks; after laptop sleep those chunks resume
    much later. Marking this locally avoids a race where daemon checks health
    before heartbeat_loop has had a chance to write its sleep_gap event.
    """
    global last_wake_time
    gap = slept_for_s - expected_s
    if gap >= SLEEP_GAP_THRESHOLD:
        last_wake_time = time.time()
        log("INFO", f"Host sleep/wake gap detected ({int(gap)}s beyond expected)")
        return gap
    return 0.0


def _in_wake_grace(now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    return bool(last_wake_time and 0 <= now - last_wake_time < WAKE_GRACE_SECONDS)


def _observe_absence(gap_seconds: float) -> None:
    """Account for host sleep and hand the owner a receipt when it mattered.

    The grace paths above are deliberately quiet about sleep — a restart or a
    component page over a lid-close would be wrong. But quiet about the
    *restart* became quiet about the absence itself: 08-17→08-19 the host was
    gone 39h, this daemon said so 38 times in its own log, and Pascal found
    out from an audit. Sleep is normal; a working day spent asleep is worth
    one card (policy lives in core.absence).

    Recording every episode is also what lets core.components age against
    time the host was actually up instead of excusing staleness by the clock.
    """
    try:
        from core import absence, hostclock

        if gap_seconds:
            hostclock.record(JARVIS_DIR, gap_seconds)
        report = absence.observe(JARVIS_DIR, gap_seconds, time.time())
        if report is None:
            return
        hours = report.slept_seconds / 3600
        accepted = absence.emit(JARVIS_DIR, report)
        log("INFO", f"Absence receipt for {hours:.1f}h host sleep "
                    f"({report.gaps} gaps): "
                    f"{'accepted' if accepted else 'not delivery-confirmed'}")
    except Exception as e:
        log("ERROR", f"Absence receipt failed: {e}")


def _is_bot_alive() -> bool:
    """Check if bot.sh is alive via PID file (primary) or pgrep (fallback)."""
    return _bot_pid() is not None


def _bot_pid() -> int | None:
    """Return the live bot.sh PID for this repo, or None."""
    # Primary: check PID file (format: "PID" or "PID BOOT_TS")
    if BOT_PID_FILE.exists():
        try:
            pid = int(BOT_PID_FILE.read_text().strip().split()[0])
            os.kill(pid, 0)  # Check if process exists
            return pid
        except (ValueError, ProcessLookupError, PermissionError, IndexError):
            pass

    # Fallback: pgrep, anchored to this repo's bot.sh — a wide pattern would
    # see another project's bot.sh as "healthy" while ours is down.
    import re as _re
    try:
        r = subprocess.run(["pgrep", "-f", f"bash.*{_re.escape(str(JARVIS_DIR))}/bot\\.sh"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            try:
                return int(line.strip())
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _ps_processes() -> dict[int, tuple[int, str]]:
    """Return {pid: (ppid, command)} from ps; never raises."""
    try:
        r = subprocess.run(["ps", "ax", "-o", "pid=,ppid=,command="],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return {}
    procs: dict[int, tuple[int, str]] = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        procs[pid] = (ppid, parts[2])
    return procs


def _has_ancestor(pid: int, ancestor: int, procs: dict[int, tuple[int, str]]) -> bool:
    """True if pid is a descendant of ancestor according to the ps snapshot."""
    seen = set()
    cur = pid
    while cur and cur not in seen:
        if cur == ancestor:
            return True
        seen.add(cur)
        cur = procs.get(cur, (0, ""))[0]
    return False


_OWNED_COMPONENT_PATTERNS = {
    "heartbeat-loop": re.compile(r"(?:^|\s)-m\s+core\.heartbeat_loop(?:\s|$)"),
    "ef-stream": re.compile(r"(?:^|\s)-m\s+core\.ef_stream_loop(?:\s|$)"),
}


def _request_component_recovery(name: str) -> bool:
    """Ask the component's existing supervisor to recreate it.

    This deliberately does not start a second copy.  For bot-owned children we
    terminate only an exact command that descends from this repo's live bot;
    bot.sh's 30s watchdog performs the restart.  Unknown/foreign processes
    are never touched.  Returns whether a bounded recovery request was made.
    """
    if _in_deploy_window():
        return False
    bot_pid = _bot_pid()
    if not bot_pid:
        return False
    pattern = _OWNED_COMPONENT_PATTERNS.get(name)
    admin_path = str(JARVIS_DIR / "admin.py")
    procs = _ps_processes()
    owned: list[int] = []
    for pid, (_, cmd) in procs.items():
        matches = bool(pattern.search(cmd)) if pattern else (
            name == "admin :3456" and admin_path in cmd)
        if matches and _has_ancestor(pid, bot_pid, procs):
            owned.append(pid)
    if not owned:
        # The bot watchdog may already be between noticing the exit and
        # spawning the replacement.  That still counts as recovery underway.
        log("INFO", f"Guardian found no live owned {name} process; "
            "owner watchdog will recreate it")
        return name in {"heartbeat-loop", "ef-stream", "admin :3456"}
    requested = False
    for pid in owned:
        try:
            os.kill(pid, signal.SIGTERM)
            requested = True
        except ProcessLookupError:
            requested = True
        except (PermissionError, OSError) as exc:
            log("WARN", f"Guardian refused/failed to recycle owned {name} "
                f"pid {pid}: {type(exc).__name__}")
    if requested:
        log("INFO", f"Guardian requested {name} recovery via its owner watchdog")
    return requested


def _is_lark_listener_alive(bot_pid: int | None = None) -> bool:
    """Check for a Lark listener owned by the current bot process.

    A stale/orphaned sidecar is worse than no sidecar: it can make the daemon
    think the bot is healthy while admin/heartbeat are gone. Anchor listener
    health to the live bot PID instead of a broad pgrep.
    """
    bot_pid = bot_pid or _bot_pid()
    if not bot_pid:
        return False
    procs = _ps_processes()
    for pid, (_, cmd) in procs.items():
        if ("lark_event_sidecar.py" in cmd or "lark-cli event" in cmd) \
                and _has_ancestor(pid, bot_pid, procs):
            return True
    return False


# check_health's issue strings are internal identifiers (logs, tests,
# conversation_audit all match on them). Anything that reaches a card goes
# through here first: Pascal reads 「bot.sh is not running」as noise, and the
# card-style contract says every user-facing line is plain Chinese.
_ISSUE_TEXTS = {
    "bot.sh is not running": "机器人主进程停了",
    "Lark event listener is not running": "飞书消息监听停了",
    "No heartbeat found in any log file": "心跳完全没有动静",
}
_ISSUE_STALE_RE = re.compile(r"^Heartbeat stale \((\d+)s since last beat\)$")


def _issue_text(issue: str) -> str:
    """One internal issue string → one plain-Chinese line."""
    issue = str(issue or "").strip()
    if issue in _ISSUE_TEXTS:
        return _ISSUE_TEXTS[issue]
    match = _ISSUE_STALE_RE.match(issue)
    if match:
        minutes = max(1, int(match.group(1)) // 60)
        return f"心跳停了 {minutes} 分钟"
    # Unknown issue: keep the raw text rather than hide the incident, but say
    # plainly that it is internal wording so the card never pretends to be
    # written for him.
    return f"另有异常（原文）：{issue}"


def _issues_block(issues: list) -> str:
    return "\n".join(f"- {_issue_text(i)}" for i in issues)


def check_health() -> dict:
    """Run health checks. Returns {"healthy": bool, "issues": [str]}."""
    issues = []

    # 0. Deploy guard (REQ-42): while restart.sh is mid-deploy the stack is
    # legitimately half-down — a daemon "fix" here is friendly fire (6/12: the
    # daemon killed a healthy bot twice during the 17:24-17:54 deploy window
    # and latched 'manual intervention needed'). A .deploying flag younger
    # than 30min means hands off; staler flags are leftovers and are ignored.
    deploying = JARVIS_DIR / ".deploying"
    if deploying.exists():
        try:
            if time.time() - deploying.stat().st_mtime < 1800:
                return {"healthy": True, "issues": [],
                        "note": "deploy window — checks suspended"}
            deploying.unlink(missing_ok=True)  # stale leftover
        except OSError:
            pass

    # 1. Is bot.sh running? (PID file + pgrep)
    bot_pid = _bot_pid()
    if not bot_pid:
        issues.append("bot.sh is not running")

    # 2. Is Lark listener connected?
    if bot_pid and not _is_lark_listener_alive(bot_pid):
        issues.append("Lark event listener is not running")

    # 3. Is heartbeat alive? (check BOTH log files for recent beat)
    # The beat-age-None case gets the SAME guards as the stale case (red-team
    # 7/8): with beats throttled, a busy conversation's own log burst can push
    # the newest beat out of the tail window — "no beat found" during an
    # active session or right after wake is the traffic's fault, not the
    # heartbeat's, and restarting would kill the in-flight user reply.
    note = None
    beat_age = _find_last_heartbeat()
    if beat_age is None or beat_age > HEARTBEAT_STALE_THRESHOLD:
        # Check if a user message is being processed (session lock exists).
        # Long Claude calls for user conversations (5-10 min) block heartbeat.
        # Restarting during an active conversation KILLS the user's response.
        import glob as _glob
        active_locks = _glob.glob(str(JARVIS_DIR / ".session_lock_*"))
        age_txt = "not found" if beat_age is None else f"stale ({int(beat_age)}s)"
        if active_locks:
            log("INFO", f"Heartbeat {age_txt} but {len(active_locks)} "
                f"session(s) active — NOT restarting (would kill user's response)")
            note = "session-active"
        elif _in_wake_grace():
            log("INFO", f"Heartbeat {age_txt} but host just woke — "
                f"grace {WAKE_GRACE_SECONDS}s, NOT restarting")
            note = "wake-grace"
        elif beat_age is None:
            issues.append("No heartbeat found in any log file")
        else:
            issues.append(f"Heartbeat stale ({int(beat_age)}s since last beat)")

    # 4. Fatal error detection REMOVED.
    # The old regex matched "Traceback" anywhere in the log tail, including
    # inside structured JSON log messages that merely REPORT script errors.
    # This caused false restarts (e.g. eigenflux_profile_post.py traceback
    # logged by heartbeat._log → daemon saw "Traceback" → restart).
    # The bot/heartbeat/lark health checks above are sufficient.
    # Script-level errors are handled by circuit breaker, not daemon restarts.

    # A suppressed heartbeat issue is FAKE-healthy: the note (mirroring the
    # .deploying note above) keeps the main loop's breaker auto-unlatch from
    # firing on it (red-team 7/8: a Lark reply's session lock suppressed the
    # stale heartbeat, the note-less healthy result cleared the latch, and
    # the restart storm the latch exists to stop resumed).
    result = {"healthy": len(issues) == 0, "issues": issues}
    if note:
        result["note"] = note
    return result


# Components stay owned by bot.sh/launchd. Guardian may request a bounded
# recovery from that existing owner, but it never starts an unsupervised copy.
# One Lark line per persistent incident per 4h.
_probe_alert_stamps: dict = {}
PROBE_ALERT_WINDOW = 4 * 3600
# The stamps must survive hot-reload respawns (REQ-42): on 7/7 the daemon
# respawned 5x in 13min during a deploy burst — each reincarnation wiped the
# dict and could re-page 组件失联 immediately instead of once per 4h. Same
# load/save robustness as RESTART_STATE_FILE; the dict above stays the live
# in-memory store.
PROBE_ALERT_STATE_FILE = JARVIS_DIR / "data" / ".daemon_probe_alert_state.json"


def _load_probe_alert_stamps():
    """Restore the probe-alert dedup stamps. Corrupt/missing file → empty."""
    try:
        loaded = json.loads(PROBE_ALERT_STATE_FILE.read_text())
        stamps = {str(k): float(v) for k, v in loaded.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        stamps = {}
    _probe_alert_stamps.clear()
    _probe_alert_stamps.update(stamps)


def _save_probe_alert_stamps():
    """Persist on every stamp mutation. Never raises — a full disk must not
    take down the alert path itself."""
    try:
        PROBE_ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROBE_ALERT_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_probe_alert_stamps))
        os.replace(tmp, PROBE_ALERT_STATE_FILE)
    except (OSError, ValueError, TypeError):
        pass

# Persisted across daemon hot-reloads: per-priority-task failure-window samples
# (so a brain-death verdict survives the REQ-42 respawn churn) + the last
# brain-death alert time (4h dedup). See _check_brain_health / core.brain_health.
BRAIN_STATE_FILE = JARVIS_DIR / ".daemon_brain_state.json"


def _in_deploy_window() -> bool:
    """True while a restart.sh deploy is in progress (.deploying < 30min old).
    Shared by check_health and probe_observed_components so the deploy guard
    can't drift between them (red-team fix)."""
    deploying = JARVIS_DIR / ".deploying"
    try:
        return deploying.exists() and time.time() - deploying.stat().st_mtime < 1800
    except OSError:
        return False


def _health_payload(body: bytes) -> dict | None:
    """Parse a /health response body. Returns the payload iff it is a health
    report (JSON dict with a "status" field) — i.e. the component answered
    with its own diagnosis and is therefore alive. Never raises."""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict) and "status" in payload:
        return payload
    return None


# What each component is called when talking to Pascal — alerts carry these,
# never the internal names/raw /health fields (feedback 7/8: he should not
# have to decode status=degraded / priority_wedged=[...]).
# Guardian messages are receipts after bounded self-repair, not requests for
# Pascal to operate the machine.  Exact diagnostics stay in daemon.log.
_REPAIR_FAILED_TAIL = "我已经自动重启并复查过，仍未恢复；我会继续处理，你暂时不用操作。"
_REPAIR_UNAVAILABLE_TAIL = "我已经复查并保留了诊断证据，但自动恢复请求没有成功；这次需要人工排查。"
_DEGRADED_TAIL = "系统已经在自动重试和切换备用路径；我会继续观察，你暂时不用操作。"

_COMPONENT_LABELS = {
    "admin :3456": "管理面板",
    "ef-stream": "EigenFlux 实时消息接收",
    "heartbeat-loop": "心跳调度",
    "lark-sidecar": "飞书事件监听",
    "bot": "机器人主进程",
}


def _note_degraded_health(name: str, payload: dict | None, http_code=None):
    """Log + alert (never restart) a component that answered its /health probe
    with a non-ok status. Separate dedup key from the DOWN alert so degraded
    chatter can't swallow a later genuine 失联 page.

    priority_wedged alone never pages HERE: _check_brain_health owns the
    wedged-PRIORITY-task alert via core/brain_health.py's detector 3, which
    was ADDED 7/9 to make that ownership real — red-team proved the original
    claim false against the 7/8 intention-check wedge (16:04→23:37: admin
    flagged priority_wedged for 7.5h, zero BRAIN-DEAD pages, and this
    degraded page was the only Lark signal). Detector 3 now mirrors admin's
    exact wedge rule, so suppressing here is a dedup, not a deletion. A
    second Lark line for the same condition was a duplicate channel
    (Pascal, 7/8). Raw fields stay in the log line; the Lark text is plain
    Chinese only."""
    key = f"{name}|degraded"
    if payload is None or payload.get("status") == "ok":
        if _probe_alert_stamps.pop(key, None) is not None:  # recovered, re-arm
            _save_probe_alert_stamps()
        return
    detail = f"status={payload.get('status')}"
    if http_code:
        detail += f" (HTTP {http_code})"
    for field in ("circuits_open", "priority_wedged", "error"):
        if payload.get(field):
            detail += f" {field}={payload[field]}"
    log("WARN", f"Observed component DEGRADED (alive): {name} — {detail}")
    reasons = []
    circuits = payload.get("circuits_open") or []
    if circuits:
        shown = "、".join(str(c) for c in circuits[:5])
        more = " 等" if len(circuits) > 5 else ""
        reasons.append(f"有定时任务连续失败、暂时停跑了（{shown}{more}）")
    if payload.get("bot_alive") is False:
        reasons.append("它看不到机器人主进程")
    if payload.get("error"):
        reasons.append("自检的时候报了错")
    if not reasons:
        if payload.get("priority_wedged"):
            # Wedge-only: the log line above is enough — brain_health's
            # detector 3 (same rule as admin's flag) pages it with the task
            # named in plain Chinese, 4h dedup + wake grace included.
            return
        reasons.append("没说具体原因")
    label = _COMPONENT_LABELS.get(name, name)
    if time.time() - _probe_alert_stamps.get(key, 0) >= PROBE_ALERT_WINDOW:
        _probe_alert_stamps[key] = time.time()
        _save_probe_alert_stamps()
        notify_lark(f"⚠️ {label}还在跑，但它自己报告有问题："
                    f"{'；'.join(reasons)}。{_DEGRADED_TAIL}",
                    incident_key=f"component:{name}:degraded")


_DEFAULT_DEADLETTER_FILE = JARVIS_DIR / "data" / ".delivery_deadletter.jsonl"
DEADLETTER_FILE = _DEFAULT_DEADLETTER_FILE

_DEADLETTER_KIND_LABELS = {
    "delivery_failures": "消息发送连续失败",
    "memorial_queue_expired": "奏折卡过期没送出去",
    "night_queue_expired": "攒批消息过期没送出去",
    "night_queue_undeliverable": "攒批消息无法投递",
    "provider_failover": "模型通道切换通知",
    "reply_send_failed": "对话回复没送出去",
    "ef_stream_send_failed": "EigenFlux 实时消息没送出去",
}
# An unlabelled kind used to render its own snake_case identifier into the
# card (memorial_queue_expired had no label and shipped like that). The kind
# still goes to the log; the card gets a sentence.
_DEADLETTER_FALLBACK_LABEL = "有消息没送出去"
# Producer details are written for whoever reads the JSONL, not for Pascal:
# ef-stream dead-letters the raw message body (a card's JSON when a card
# failed), and delivery_failures writes English. A boss-facing card carries
# neither (feedback-card-style-contract, feedback-no-jargon-dashboards).
_DETAIL_TRANSLATIONS = (
    (re.compile(r"^(\d+) consecutive send failures$"), r"连续 \1 次没发出去"),
)


def _humanize_detail(detail: str) -> str:
    """Producer detail → one short Chinese fragment, or '' to say nothing.

    Dropping a detail is deliberate: the label already names what went wrong,
    and a truncated JSON blob adds nothing he can act on.
    """
    text = " ".join(str(detail or "").split())
    if not text:
        return ""
    if text.startswith(("{", "[")) or '":' in text or '": ' in text:
        return ""
    for pattern, replacement in _DETAIL_TRANSLATIONS:
        if pattern.match(text):
            return pattern.sub(replacement, text)
    # Anything still mostly ASCII is internal wording (English error strings,
    # module names). Keep short Chinese details, drop the rest.
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    if ascii_chars > len(text) * 0.7:
        return ""
    return text[:60]


def consume_delivery_deadletters():
    """Raise alarms the heartbeat loop dead-lettered for us (stability
    backlog #7, consumer half — producer contract in
    core/delivery_deadletter.py): the loop's own delivery alert rides the
    failing channel from the possibly-dead loop, so it also writes each
    overdue delivery here for the independent Guardian process to retry.

    Guardian is an independent process path, not an independent channel: its
    Lark delivery can fail with Lark.  We therefore snapshot under flock,
    notify, and consume only the accepted snapshot.  A genuine send loss
    leaves the source evidence intact; concurrent appends are preserved.
    Never raises.
    """
    try:
        # SQLite is authoritative for all new envelopes. Mark rows notified
        # only after the Guardian delivery is confirmed; an outage leaves
        # them pending for the next pass.
        try:
            from core.delivery import DeliveryPipeline
            pipeline = DeliveryPipeline(JARVIS_DIR)
            sql_rows = (
                pipeline.pending_dead_letters(100)
                if DEADLETTER_FILE == _DEFAULT_DEADLETTER_FILE else []
            )
            if sql_rows:
                grouped: dict[tuple[str, str], int] = {}
                for row in sql_rows:
                    key = (
                        str(row.get("source") or "unknown"),
                        str(row.get("kind") or "message"),
                    )
                    grouped[key] = grouped.get(key, 0) + 1
                detail_lines = [
                    f"- {source}：{count} 条"
                    for (source, _kind), count in sorted(grouped.items())
                ]
                delivered = notify_lark(
                    f"⚠️ 有 {len(sql_rows)} 条消息最终未送达。失败记录还在；"
                    "投递恢复后只补发仍有效、仍未处理的事项，过期提醒不会补发：\n"
                    + "\n".join(detail_lines),
                    incident_key="delivery-terminal",
                )
                # SQLite rows remain authoritative until the Guardian notice
                # is confirmed or covered by an already confirmed incident.
                if delivered:
                    pipeline.mark_dead_letters_notified(
                        [int(row["id"]) for row in sql_rows])
        except Exception as e:
            log("ERROR", f"sqlite deadletter consume failed: {e}")

        if not DEADLETTER_FILE.exists() or DEADLETTER_FILE.stat().st_size == 0:
            return
        import fcntl
        with open(DEADLETTER_FILE, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                snapshot = f.read()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        lines = snapshot.splitlines()
        by_kind: dict = {}
        for ln in lines:
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            by_kind.setdefault(str(row.get("kind", "unknown")), []).append(row)
        if not by_kind:
            outcomes: list[bool | None] = []
        else:
            outcomes = []
        n = sum(len(v) for v in by_kind.values())
        log("WARN", f"Delivery dead-letters: {n} row(s), kinds={sorted(by_kind)}")
        # provider_failover 是状态通知，不是没送出去的消息 —— 内容本身就是
        # 给 Pascal 的一句人话（“已切到备用通道”/“已切回”），套“消息没送
        # 出去”的帽子是在报假警（red-team 7/8）。单独原文发出，不截断
        # （producer 端已限 500 字）；同一批里新旧几条只发最新一条 ——
        # trip+clear 同批时，把已过时的“切换”播报补发出去等于恢复后又喊
        # 一次故障。其余 kind 保持原来的打包告警不变。
        failover = by_kind.pop("provider_failover", None)
        if failover:
            detail = str(failover[-1].get("detail", "")).strip()
            if detail.startswith(("{", "[")):
                detail = ""  # raw payload never goes out as a card body
            outcomes.append(notify_lark(
                detail or _DEADLETTER_KIND_LABELS["provider_failover"],
                incident_key="provider-failover",
            ))
        if by_kind:
            parts = []
            for kind, items in sorted(by_kind.items()):
                label = _DEADLETTER_KIND_LABELS.get(
                    kind, _DEADLETTER_FALLBACK_LABEL)
                first = items[0]
                extra = f"（共 {len(items)} 条）" if len(items) > 1 else ""
                since = str(first.get("due_since", "")).strip()
                since_txt = f"，自 {since} 起" if since else ""
                detail = _humanize_detail(first.get("detail", ""))
                detail_txt = f"：{detail}" if detail else ""
                parts.append(f"- {label}{extra}{since_txt}{detail_txt}")
            outcomes.append(notify_lark(
                "⚠️ 有本该送到你手上的消息没送出去。失败记录仍然保留；"
                "飞书恢复后只会补仍有效的事项：\n" + "\n".join(parts),
                incident_key="delivery-legacy",
            ))

        # Only remove the exact snapshot after every notice was accepted by
        # the durable pipeline (None means queued).  A genuine loss keeps the
        # source evidence for the next pass.  Concurrent appends survive.
        if all(outcome is not False for outcome in outcomes):
            with open(DEADLETTER_FILE, "r+", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    current = f.read()
                    if current.startswith(snapshot):
                        remainder = current[len(snapshot):]
                        f.seek(0)
                        f.write(remainder)
                        f.truncate()
                    else:
                        log("WARN", "legacy deadletter changed before consume; "
                            "leaving evidence intact")
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        log("ERROR", f"deadletter consume failed: {e}")


def _clear_probe_keys(*keys: str) -> None:
    changed = False
    for key in keys:
        changed |= _probe_alert_stamps.pop(key, None) is not None
    if changed:
        _save_probe_alert_stamps()


def probe_observed_components():
    """Repair, verify, then alert when :3456 remains down."""
    # During a restart window the stack is legitimately down — probing here
    # would fire false 'component DOWN' alerts (red-team fix: the probe ran
    # OUTSIDE check_health's deploy guard).
    if _in_deploy_window():
        return
    import urllib.request
    import urllib.error
    for name, url in (("admin :3456", "http://127.0.0.1:3456/health"),):
        alive = False
        payload = None
        code = None
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                code = resp.status
                alive = resp.status < 500
                if alive:
                    # admin convention A: 200 with {"status": "degraded"} body
                    payload = _health_payload(resp.read())
        except urllib.error.HTTPError as e:
            # The server RESPONDED (admin convention B: /health 503 with a
            # JSON body when status=="error"). A health-JSON body means it is
            # alive-but-degraded, not 失联 — never page "探测不通" for this.
            # Only connection failures/timeouts and non-JSON 5xx (crashed
            # handler, proxy error page) fall through to the DOWN alert.
            code = e.code
            try:
                payload = _health_payload(e.read())
            except Exception:
                payload = None
            alive = payload is not None or e.code < 500
        except Exception:
            pass
        pending_key = f"{name}|pending"
        repair_key = f"{name}|repair"
        if alive:
            _clear_probe_keys(name, pending_key, repair_key)
            _note_degraded_health(name, payload, code)
            continue
        now = time.time()
        first_seen = _probe_alert_stamps.get(pending_key, 0)
        if not first_seen:
            _probe_alert_stamps[pending_key] = now
            if _request_component_recovery(name):
                _probe_alert_stamps[repair_key] = now
            _save_probe_alert_stamps()
            log("INFO", f"Observed component red (1st probe); recovery "
                f"requested before alert: {name}")
            continue
        if now - first_seen < COMPONENT_RECOVERY_GRACE:
            continue
        last = _probe_alert_stamps.get(name, 0)
        if now - last >= PROBE_ALERT_WINDOW:
            _probe_alert_stamps[name] = now
            _save_probe_alert_stamps()
            log("WARN", f"Observed component DOWN after recovery: {name}")
            label = _COMPONENT_LABELS.get(name, name)
            tail = (_REPAIR_FAILED_TAIL if _probe_alert_stamps.get(repair_key)
                    else _REPAIR_UNAVAILABLE_TAIL)
            notify_lark(f"⚠️ {label}连续两次连不上。{tail}",
                        incident_key=f"component:{name}:down")


# Manifest criticals that check_health / probe_observed_components already
# watch through richer paths: bot/heartbeat-loop/lark-sidecar drive the restart
# path, and admin gets the degraded-body-aware probe above (which the
# manifest's plain status<500 check would regress). The manifest probe
# only adds what nothing else covers — today that is ef-stream.
_MANIFEST_COVERED = {"bot", "heartbeat-loop", "lark-sidecar", "admin"}


def _component_down_text(label: str, detail: str,
                         recovery_requested: bool = True) -> str:
    """What to tell Pascal about a red manifest-critical component.

    Every red used to render as 「组件失联：X 没有在运行」. On 2026-08-18 02:16
    that sentence went out about ef-stream while the ef-stream process was
    alive and owned — the host had been asleep and the health file had gone
    stale. It was the only alert to reach him in 39 hours and it pointed at
    the wrong thing. A check that verified liveness says so, and we repeat
    what it found rather than diagnosing on its behalf.
    """
    try:
        from core.components import ALIVE_BUT_SILENT
    except Exception:
        ALIVE_BUT_SILENT = "进程在跑但没在报状态"
    tail = (_REPAIR_FAILED_TAIL if recovery_requested
            else _REPAIR_UNAVAILABLE_TAIL)
    if ALIVE_BUT_SILENT in detail:
        return (f"⚠️ {label}不太对劲：进程还活着，但很久没报状态了。"
                f"{tail}")
    # 「组件失联：X没有在运行」said the same thing twice and led with the word
    # 「组件」—— the label already names the thing in his vocabulary.
    return f"⚠️ {label}停了，进程已经不在了。{tail}"


def _probe_manifest_criticals():
    """Repair, verify, then alert on dead manifest-critical components.
    The manifest promised 'critical: true → daemon checks it' but the daemon
    kept its own hardcoded list, so critical ef-stream had no daemon coverage
    on any path (audit 7/8). Owner-mediated, deploy-guarded, same persisted 4h
    dedup as the hardcoded probes, debounced across two consecutive red
    probes so a watchdog-healed transient never pages (red-team 7/9).
    Never raises."""
    if _in_deploy_window():
        return
    try:
        # Right after our own diagnose_and_fix restart the children are
        # legitimately still coming up; and with bot.sh itself down every
        # owned_by_pidfile check is red — that outage belongs to the restart
        # path, not to a page per child.
        if time.time() - last_restart_time < RESTART_COOLDOWN:
            return
        if not _bot_pid():
            return
        from core.components import check_components
        for r in check_components(critical_only=True):
            name = str(r.get("name", "?"))
            if name in _MANIFEST_COVERED:
                continue
            pending_key = f"{name}|pending"
            repair_key = f"{name}|repair"
            if r.get("ok"):
                # Recovered: re-arm the 4h dedup AND clear any pending
                # first-seen mark, so a healed transient never pages later.
                _clear_probe_keys(name, pending_key, repair_key)
                continue
            # Debounce (red-team 7/9): bot.sh's watchdog respawns a dead
            # ef-stream within ≤30s, and a single crash landing inside a
            # probe used to page 组件失联 for a component that healed seconds
            # later — with the 4h-sticky dedup meaning nothing corrected it.
            # First red probe only marks the component pending (persisted, so
            # hot-reload respawns keep the mark); the page needs a SECOND
            # consecutive red probe >= MANIFEST_CONFIRM_WINDOW later.
            first_seen = _probe_alert_stamps.get(pending_key, 0)
            if not first_seen:
                now = time.time()
                _probe_alert_stamps[pending_key] = now
                if _request_component_recovery(name):
                    _probe_alert_stamps[repair_key] = now
                _save_probe_alert_stamps()
                log("INFO", f"Manifest-critical component red (1st probe, "
                    f"recovery requested, awaiting confirmation): {name} — "
                    f"{r.get('detail')}")
                continue
            if time.time() - first_seen < MANIFEST_CONFIRM_WINDOW:
                continue
            if time.time() - _probe_alert_stamps.get(name, 0) >= PROBE_ALERT_WINDOW:
                _probe_alert_stamps[name] = time.time()
                _save_probe_alert_stamps()
                log("WARN", f"Manifest-critical component DOWN: {name} — "
                    f"{r.get('detail')}")
                label = _COMPONENT_LABELS.get(name, name)
                notify_lark(
                    _component_down_text(
                        label, str(r.get("detail") or ""),
                        recovery_requested=bool(
                            _probe_alert_stamps.get(repair_key)),
                    ),
                    incident_key=f"component:{name}:down",
                )
    except Exception as e:
        log("ERROR", f"manifest component probe failed: {e}")


def _network_reachable() -> bool:
    """2s TCP dial to the Lark endpoint — the daemon's own alert channel.
    False ⇒ the host is offline (flight / captive portal / hotspot switch),
    where every network-dependent task fails and the brain-death heuristics
    lie: that is a connectivity outage, not brain-death. Note the diag-
    staleness leg needs no such gate — it watches a local file mtime, not
    network-dependent task failures."""
    try:
        with socket.create_connection(REACHABILITY_PROBE,
                                      timeout=REACHABILITY_TIMEOUT):
            return True
    except OSError:
        return False


def _check_brain_health():
    """Observe, verify, then alert when heartbeat is ALIVE BUT BRAIN-DEAD —
    ticking every cycle while every claude_call fails. On 2026-06-15 `claude`
    was missing from the launchd PATH for ~1h and EVERY liveness signal stayed
    fresh (beat-marker, /health heartbeat_age, per-task circuit), so nothing
    caught it. The daemon is Claude-independent and can: it reads
    heartbeat_state.json directly and applies core/brain_health.py's three
    detectors (ran-but-failing starvation, priority failure windows, and the
    priority WEDGE rule mirrored from admin /health — the 7/8 intention-check
    wedge class whose only page used to be the deleted degraded-channel
    alert). Task failures are not process failures: provider overload, timeout
    and parsing errors must stay with the provider chain and scheduler retry,
    never kill the whole heartbeat loop. It waits a full task-cycle grace and
    pages only if verification is still red. 4h dedup, deploy-guarded. Never
    raises."""
    if _in_deploy_window():
        return
    try:
        # A legitimately stopped loop is the bot-alive check's job, not ours —
        # only judge brain-death while the loop is actually ticking.
        beat_age = _find_last_heartbeat()
        if beat_age is None or beat_age > HEARTBEAT_STALE_THRESHOLD:
            return

        from core import brain_health
        from core.heartbeat import HeartbeatRunner, parse_heartbeat
        from core.task_protocol import CircuitState

        try:
            state = json.loads((JARVIS_DIR / "heartbeat_state.json").read_text())
        except (OSError, ValueError):
            return
        try:
            overrides = json.loads(
                (JARVIS_DIR / "interval_overrides.json").read_text())
        except (OSError, ValueError):
            overrides = {}
        tasks = parse_heartbeat(JARVIS_DIR / "HEARTBEAT.md")

        try:
            prev = json.loads(BRAIN_STATE_FILE.read_text())
        except (OSError, ValueError):
            prev = {}
        prev_samples = prev.get("samples", {}) or {}
        last_alert = prev.get("last_alert", 0) or 0
        last_check_ts = prev.get("last_check_ts", 0) or 0
        grace_until = prev.get("grace_until", 0) or 0
        repair_requested_at = prev.get("repair_requested_at", 0) or 0
        repair_request_ok = bool(prev.get("repair_request_ok", True))
        # Suppression ledger (audit 7/10): {"since": ts, "windows": n} — how
        # long / across how many awake windows the SAME brain-dead verdict
        # has been swallowed by grace. Persisted so it survives sleeps and
        # daemon hot-reload respawns; cleared the moment a check comes back
        # healthy or the page actually goes out.
        suppressed = prev.get("suppressed") or {}

        # Post-sleep/reboot grace: a stale last_success right after the host
        # was asleep or powered off is expected, not brain-death. Two gap
        # signals — the in-process wake mark (laptop lid), and a hole in our
        # own check cadence (reboot wipes last_wake_time; this file survives).
        now = time.time()
        new_window = False
        if last_wake_time:
            grace_until = max(grace_until, last_wake_time + BRAIN_WAKE_GRACE)
        if last_check_ts and now - last_check_ts > BRAIN_CHECK_GAP_THRESHOLD:
            grace_until = max(grace_until, now + BRAIN_WAKE_GRACE)
            new_window = True
            log("INFO", f"brain-health: host sleep/shutdown gap "
                f"({int(now - last_check_ts)}s since last check) — alerts on "
                f"hold for {BRAIN_WAKE_GRACE // 60}min while heartbeat catches up")
        in_grace = now < grace_until
        if in_grace:
            # Pre-sleep failure-window deltas are meaningless across the nap;
            # rebaseline so priority windows can't accumulate during grace.
            prev_samples = {}

        result = brain_health.assess(
            state=state, tasks=tasks, overrides=overrides,
            priority_tasks=HeartbeatRunner.PRIORITY_TASKS,
            prev_samples=prev_samples, now=now,
            failure_threshold=CircuitState.FAILURE_THRESHOLD,
        )

        if not result["brain_dead"]:
            suppressed = {}
            repair_requested_at = 0
            repair_request_ok = True
        elif in_grace:
            if not suppressed:
                suppressed = {"since": now, "windows": 1}
            elif new_window:
                suppressed["windows"] = int(suppressed.get("windows", 1)) + 1
        # A verdict that crossed a sleep (second awake window) or ate a full
        # hour of suppression is a persistent failure, not a wake artefact —
        # it punches through the grace (still subject to the 4h dedup below).
        penetrates = bool(suppressed) and (
            int(suppressed.get("windows", 1)) >= BRAIN_SUPPRESS_MAX_WINDOWS
            or now - float(suppressed.get("since", now)) >= BRAIN_SUPPRESS_MAX_SECONDS)

        new_last_alert = last_alert
        if (result["brain_dead"] and (penetrates or not in_grace)
                and now - last_alert >= PROBE_ALERT_WINDOW):
            if not _network_reachable():
                # Offline ⇒ connectivity outage, not brain-death (7/9 flight
                # day false page). Defer, and re-arm the grace so the page
                # can't fire until ~BRAIN_WAKE_GRACE past the last offline
                # check — the heartbeat needs that long to retry once the
                # network returns. Resetting the ledger keeps offline-accrued
                # suppression from penetrating right at recovery.
                grace_until = max(grace_until, now + BRAIN_WAKE_GRACE)
                suppressed = {}
                log("INFO", "brain-health: would alert but host looks offline "
                    "(Lark endpoint unreachable) — deferring; grace re-armed "
                    f"for {BRAIN_WAKE_GRACE // 60}min while the network returns")
            else:
                if not repair_requested_at:
                    # This is a task-level verdict while heartbeat liveness is
                    # fresh. Restarting the process cannot repair an overloaded
                    # provider or a bad envelope; it only interrupts unrelated
                    # tasks and may load mutable, unreleased source. Start a
                    # verification window and leave recovery to the provider
                    # chain plus scheduler retry.
                    repair_requested_at = now
                    repair_request_ok = True
                    log("WARN", "BRAIN-DEAD heartbeat: verification window "
                        "started without process restart: "
                        + "; ".join(result["alerts"]))
                elif now - repair_requested_at < BRAIN_RECOVERY_GRACE:
                    log("INFO", "brain-health: recovery in progress; waiting "
                        f"{BRAIN_RECOVERY_GRACE // 60}min before alert")
                else:
                    new_last_alert = now
                    # Tag goes at the END: conversation_audit.py classifies on
                    # the literal "BRAIN-DEAD heartbeat:" prefix.
                    tag = (" [persisted across post-wake grace]" if in_grace
                           else "")
                    log("WARN", "BRAIN-DEAD heartbeat: "
                        + "; ".join(result["alerts"]) + tag)
                    summary = result["summary"]
                    notify_lark(summary,
                                incident_key="heartbeat-brain-dead")
                    suppressed = {}
        elif result["brain_dead"] and in_grace:
            log("INFO", "brain-health: would alert but in post-wake grace "
                f"(suppressed {int(now - float(suppressed.get('since', now)))}s "
                f"across {int(suppressed.get('windows', 1))} awake window(s)): "
                + "; ".join(result["alerts"]))

        # Atomic persist: samples carry the per-priority failure windows across
        # hot-reloads; last_alert enforces the 4h dedup; last_check_ts/grace_until
        # drive the sleep-gap detection above; suppressed drives grace penetration.
        new_state = {"samples": result["samples"], "last_alert": new_last_alert,
                     "last_check_ts": now, "grace_until": grace_until,
                     "suppressed": suppressed,
                     "repair_requested_at": repair_requested_at,
                     "repair_request_ok": repair_request_ok}
        tmp = BRAIN_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_state))
        os.replace(tmp, BRAIN_STATE_FILE)
    except Exception as e:
        log("ERROR", f"brain-health check failed: {e}")


def _check_diag_staleness():
    """Repair, verify, then alert when self-diagnostic — the system's only
    aggregated health alarm — stops running, or when its ⚠️ warnings die on
    the heartbeat-side delivery path (stability backlog #5: nothing watched
    the watcher, the same structural silence that let the dashboard die for
    23 days). Two legs:
      A. .diag_last_pre.txt missing/stale → the 4h task itself stopped
         (scheduler wedge, open circuit, task removed).
      B. pre fresh with ⚠️ lines but the shared alert stamp never advanced
         after the pre run → the post-script's own send failed; re-deliver
         from the independent Guardian process path through the same Lark
         channel. (The pre mtime only proves the PRE stage ran — leg B is
         what covers the delivery half.)
    Self-healing, deploy-guarded, never raises."""
    if _in_deploy_window():
        return
    try:
        now = time.time()
        # Same post-wake reasoning as brain-health: after hours of lid-close
        # every mtime is stale by the nap length; after a reboot
        # last_wake_time is wiped but our own process age gives the
        # equivalent grace (launchd starts us at login).
        if last_wake_time and now - last_wake_time < BRAIN_WAKE_GRACE:
            return
        if now - _daemon_started < BRAIN_WAKE_GRACE:
            return
        # A fully dead loop already pages through the bot/heartbeat checks —
        # a diag staleness page on top of that outage is noise.
        beat_age = _find_last_heartbeat()
        if beat_age is None or beat_age > HEARTBEAT_STALE_THRESHOLD:
            return

        try:
            pre_age = now - DIAG_PRE_FILE.stat().st_mtime
        except OSError:
            pre_age = None

        key = "self-diagnostic|stale"
        repair_key = "self-diagnostic|repair"
        repair_ok_key = "self-diagnostic|repair-ok"
        if pre_age is None or pre_age > DIAG_STALE_THRESHOLD:
            repair_at = _probe_alert_stamps.get(repair_key, 0)
            if not repair_at:
                _probe_alert_stamps[repair_key] = now
                if _request_component_recovery("heartbeat-loop"):
                    _probe_alert_stamps[repair_ok_key] = now
                _save_probe_alert_stamps()
                log("WARN", "self-diagnostic stale; heartbeat recovery "
                    "requested before alert")
                return
            if now - repair_at < DIAG_RECOVERY_GRACE:
                return
            if now - _probe_alert_stamps.get(key, 0) >= PROBE_ALERT_WINDOW:
                _probe_alert_stamps[key] = now
                _save_probe_alert_stamps()
                repair_ok = bool(_probe_alert_stamps.get(repair_ok_key))
                tail = (_REPAIR_FAILED_TAIL if repair_ok
                        else _REPAIR_UNAVAILABLE_TAIL)
                if pre_age is None:
                    log("WARN", "self-diagnostic pre report missing")
                    notify_lark("⚠️ 自诊断任务好像一直没跑成过（找不到它的"
                                f"体检报告）。{tail}",
                                incident_key="self-diagnostic-stale")
                else:
                    log("WARN", f"self-diagnostic stale "
                        f"({int(pre_age)}s since last pre run)")
                    notify_lark(f"⚠️ 自诊断任务已经 {int(pre_age // 3600)} 小时"
                                f"没有运行了（正常每 4 小时一次）。{tail}",
                                incident_key="self-diagnostic-stale")
            return
        _clear_probe_keys(key, repair_key, repair_ok_key)

        # Leg B — only on EVIDENCE the post had its turn (red-team 7/9: the
        # old 15min wall-clock grace fired mid-cycle whenever pre→post ran
        # long — a failover retry chain or 4-pre batch routinely takes
        # 16-27min — falsely paging "post delivery path dead" while the
        # warnings were merely queued behind the in-flight Claude call, and
        # the daemon's stamp write then suppressed the post's own send).
        # last_run advances only when the task's cycle FINISHES, post-script
        # routing included (and failure paths BACKDATE it by ~a full
        # interval), so last_run catching up to this pre file proves the
        # post ran for it. See DIAG_CYCLE_START_SKEW for why the comparison
        # is not a strict >=.
        pre_mtime = now - pre_age
        try:
            hb = json.loads((JARVIS_DIR / "heartbeat_state.json").read_text())
            diag_last_run = float(
                (hb.get("self-diagnostic") or {}).get("last_run", 0) or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return  # can't prove the post ran — never re-deliver on a guess
        if diag_last_run < pre_mtime - DIAG_CYCLE_START_SKEW:
            return  # cycle still in flight (or failed and awaiting fast retry)
        try:
            stamp_ts = float(json.loads(
                DIAG_ALERT_STAMP.read_text()).get("ts", 0) or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            stamp_ts = 0.0  # unreadable = never alerted, matching the post
        # Delivery failed only if, AT THE PRE RUN, the stamp was already past
        # the post's dedup window (so the post should have sent) yet it never
        # advanced. A stamp within the window before the pre means the post
        # suppressed on purpose; a stamp after the pre means it delivered.
        if pre_mtime - stamp_ts < DIAG_ALERT_WINDOW:
            return
        try:
            from tasks.self_diagnostic_post import (
                extract_warnings,
                should_alert,
                user_actionable_warnings,
                user_alert_text,
            )
        except Exception:
            return  # tasks/ tree unreadable — leg A above still stands
        warnings = extract_warnings(
            DIAG_PRE_FILE.read_text(encoding="utf-8", errors="replace"))
        actionable = user_actionable_warnings(warnings)
        if warnings and not actionable:
            # The post-script normally writes this shared receipt. If its
            # delivery leg failed, Guardian still acknowledges automatically
            # owned diagnostics without assigning engineering work to Pascal.
            try:
                previous = json.loads(DIAG_ALERT_STAMP.read_text()) or {}
            except (OSError, ValueError, TypeError):
                previous = {}
            try:
                tmp = DIAG_ALERT_STAMP.with_suffix(".tmp")
                tmp.write_text(json.dumps({
                    "ts": now,
                    "user_ts": float(previous.get("user_ts", 0) or 0),
                    "lines": warnings[:20],
                }, ensure_ascii=False))
                os.replace(tmp, DIAG_ALERT_STAMP)
            except OSError:
                pass
            log("INFO", "self-diagnostic internal warnings acknowledged; "
                "no owner action required")
            return
        # Content-aware (REQ-109): re-send only for warnings the stamped set
        # has never carried, or as a 24h reminder. The old window-only check
        # ping-ponged with the post's dedup — the same persisting warning
        # reached Pascal every 8h, each time claiming the primary path broke.
        try:
            stamp = json.loads(DIAG_ALERT_STAMP.read_text()) or {}
        except (OSError, ValueError, TypeError):
            stamp = {}
        if not should_alert(warnings, stamp):
            return
        log("WARN", f"self-diagnostic warnings unsent by post "
            f"({len(warnings)}) — sending via daemon channel")
        receipt = notify_lark(
            user_alert_text(actionable),
            incident_key="self-diagnostic-delivery",
        )
        if receipt is False:
            return
        try:
            tmp = DIAG_ALERT_STAMP.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"ts": now, "user_ts": now, "lines": warnings[:20]},
                ensure_ascii=False))
            os.replace(tmp, DIAG_ALERT_STAMP)
        except OSError:
            pass
    except Exception as e:
        log("ERROR", f"diag staleness check failed: {e}")


def _remind_breaker_latched():
    """While the breaker stays latched, remind Pascal at most once per day —
    a latched breaker plus a dead stack is otherwise a silent outage (the
    latch alert itself fires exactly once). Reminder stamp lives inside the
    latch file so it dies with the latch. Never raises."""
    now = time.time()
    state: dict = {}
    try:
        loaded = json.loads(BREAKER_LATCH_FILE.read_text())
        if isinstance(loaded, dict):
            state = loaded
    except (OSError, ValueError):
        pass
    try:
        last_ping = float(state.get("last_reminder", 0) or 0)
    except (TypeError, ValueError):
        last_ping = 0.0
    if now - last_ping < 24 * 3600:
        return
    state["last_reminder"] = now
    try:
        tmp = BREAKER_LATCH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False))
        os.replace(tmp, BREAKER_LATCH_FILE)
    except OSError:
        return  # can't record the stamp — skip rather than page every check
    log("WARN", "Restart breaker still latched — daily reminder sent")
    notify_lark("⚠️ 提醒：我这边的自动重启还停着（之前连续重启失败后暂停的），"
                "系统可能还没恢复。我会继续保留诊断证据并每天复查一次；"
                "这次确实需要人工排查。",
                incident_key="bot-restart-breaker")


def _clear_breaker_latch(reason: str):
    """Re-arm a latched restart breaker: delete the flag + refresh the budget.
    Called when the stack is observed healthy again — without an automatic
    clear, a transient incident whose post-checks failed (slow warm-up) would
    disable the auto-restart safety net forever. Never raises."""
    global restart_count, last_restart_time
    if not BREAKER_LATCH_FILE.exists():
        return
    try:
        BREAKER_LATCH_FILE.unlink()
    except OSError:
        return
    restart_count = 0
    last_restart_time = 0
    _save_restart_state()
    log("INFO", f"Restart breaker re-armed ({reason})")
    notify_lark("✅ 系统恢复正常了，我已经解除自动重启的暂停，恢复正常看护。",
                incident_key="bot-restart-recovered")


def _maybe_clear_breaker_latch(result: dict):
    """Re-arm a latched breaker only on a GENUINELY healthy check. ANY "note"
    marks fake-healthy (deploy window / active session / wake grace) — those
    suppress issues without observing recovery, so unlatching on them re-pages
    a false '恢复正常' and re-enables the restart storm the latch exists to
    stop (red-team 7/8)."""
    if result.get("healthy") and "note" not in result:
        _clear_breaker_latch("system healthy again")


def _maybe_reset_restart_budget(result: dict):
    """Zero the persisted restart budget after a sustained healthy stretch —
    same fake-healthy "note" gate as _maybe_clear_breaker_latch. Without this,
    an increment whose 90s post-check merely missed a slow warm-up outlives
    the incident in RESTART_STATE_FILE, and leaks from unrelated incidents
    weeks apart eventually latch the breaker with zero restarts attempted.
    The window comparison also holds when last_restart_time sits in the
    FUTURE (failed-latch-write fallback stores now+600): the delta goes
    negative, which never clears the window."""
    global restart_count
    if restart_count <= 0:
        return
    if not result.get("healthy") or "note" in result:
        return
    if time.time() - last_restart_time < RESTART_BUDGET_RESET_WINDOW:
        return
    log("INFO", f"Restart budget reset after sustained healthy stretch "
        f"(was {restart_count}/{MAX_RESTART_ATTEMPTS})")
    restart_count = 0
    _save_restart_state()


def _session_lock_pid_is_ours(pid: int) -> bool:
    """Identity check before killing a session-lock PID (same class af35420
    fixed in bot.sh cleanup()): lock files outlive crashed handlers, and a
    recycled PID may belong to an arbitrary user process. The lock stores the
    $! of a backgrounded bot.sh pipeline subshell, so ps shows the parent argv
    ('bash .../bot.sh') — never 'claude'; match ONLY the repo's bot.sh path.
    (An `or "claude" in args` fallback was removed 7/8 red-team: a legitimate
    lock holder can never show 'claude', so the substring arm could only ever
    match a recycled PID landing on someone ELSE's claude process — e.g.
    Pascal's interactive Claude Code session — the exact wrong-kill this check
    exists to prevent, and the substring-matching class the 7/7 watchdog
    postmortem banned. If the subshell ever execs claude directly, add an
    anchored full-path match then.)"""
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                           capture_output=True, text=True, timeout=5)
        args = r.stdout.strip()
    except Exception:
        return False
    if not args:
        return False
    return bool(re.search(rf"bash.*{re.escape(str(JARVIS_DIR))}/bot\.sh", args))


def diagnose_and_fix(issues: list[str]) -> str:
    """Kill existing processes and restart bot.sh."""
    global last_restart_time, restart_count

    now = time.time()

    # Breaker latched: hold ALL auto-restarts until a human deletes the flag
    # or the main loop observes a genuinely healthy check (7/7 audit: the old
    # max-attempts branch re-armed itself, giving a permanently broken stack
    # 3 restart storms + 1 page per ~35min forever).
    if BREAKER_LATCH_FILE.exists():
        _remind_breaker_latched()
        return "restart breaker latched — waiting for manual re-arm"

    # Cooldown check
    if now - last_restart_time < RESTART_COOLDOWN:
        remaining = int(RESTART_COOLDOWN - (now - last_restart_time))
        log("INFO", f"Restart cooldown active ({remaining}s remaining)")
        return f"restart cooldown ({remaining}s remaining)"

    if restart_count >= MAX_RESTART_ATTEMPTS:
        msg = (f"Reached max restart attempts ({MAX_RESTART_ATTEMPTS}) — "
               f"breaker latched, auto-restart disabled until manual re-arm")
        log("ERROR", msg)
        latched = False
        try:
            BREAKER_LATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = BREAKER_LATCH_FILE.with_suffix(".tmp")
            # last_reminder starts now: the latch alert below IS today's page
            tmp.write_text(json.dumps(
                {"latched_at": now,
                 "latched_at_human": _daemon_now().strftime("%Y-%m-%d %H:%M:%S"),
                 "issues": issues,
                 "last_reminder": now}, ensure_ascii=False))
            os.replace(tmp, BREAKER_LATCH_FILE)
            latched = True
        except OSError:
            pass
        notify_lark(f"⚠️ 我连续重启了 {MAX_RESTART_ATTEMPTS} 次都没能把系统救回来，"
                    "为了不无限折腾，我已经暂停自动重启，需要你人工看一下。\n\n"
                    "问题：\n" + _issues_block(issues) + "\n\n"
                    "我会每天复查一次；系统自己恢复后也会自动解除并告诉你。",
                    incident_key="bot-restart-breaker")
        # The latch flag now gates all restarts, so zeroing the counter is
        # safe — deleting the flag deliberately re-arms with a fresh budget.
        restart_count = 0
        if not latched:
            # Flag write failed (disk?) — fall back to the old 10min extra
            # cooldown so this branch can't re-fire every check.
            last_restart_time = now + 600
        _save_restart_state()
        return msg

    log("INFO", f"Attempting fix for: {issues}")

    # Kill existing bot processes (including eigenflux streams which may be
    # reparented to init/gateway and survive bot.sh cleanup).
    # Patterns are anchored to JARVIS_DIR where possible so we never kill
    # unrelated processes on the same machine (a hand-run lark-cli listener,
    # another project's admin.py, ...). lark-cli/eigenflux can't be path-
    # anchored (invoked by bare name) — those stay broad but are specific
    # subcommands unlikely to exist outside this bot.
    log("INFO", "Killing existing processes...")
    import re as _re
    _jd = _re.escape(str(JARVIS_DIR))
    for pattern in ["lark-cli event|lark_event_sidecar",
                    f"bash.*{_jd}/bot\\.sh",
                    # path-anchored, interpreter-agnostic (shows up as
                    # ".../Python .../admin.py" under homebrew python)
                    f"{_jd}/admin\\.py",
                    "eigenflux stream"]:
        try:
            subprocess.run(["pkill", "-f", pattern],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    # Kill stuck claude processes tracked by session locks
    # (lock format: "<pid> <token>" — first field is the pid). Identity check
    # first: locks survive crashed handlers, and killing a recycled PID blind
    # hits an arbitrary user process (7/7 audit; same class as af35420).
    import glob as _glob
    for lock in _glob.glob(str(JARVIS_DIR / ".session_lock_*")):
        try:
            content = Path(lock).read_text().strip()
            pid = content.split()[0] if content else ""
            if pid.isdigit():
                if _session_lock_pid_is_ours(int(pid)):
                    subprocess.run(["kill", pid], capture_output=True, timeout=5)
                    log("INFO", f"Killed stuck claude process from lock: {pid}")
                else:
                    log("INFO", f"Session lock PID {pid} is not ours "
                        f"(dead or recycled) — removing lock without killing")
        except Exception:
            pass

    # Clean stale session locks and PID file
    for lock in _glob.glob(str(JARVIS_DIR / ".session_lock_*")):
        try:
            os.remove(lock)
        except Exception:
            pass
    try:
        BOT_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    time.sleep(3)

    # Start bot.sh — output to /tmp/jarvis_restart.log (daemon reads this)
    log("INFO", "Starting bot.sh...")
    try:
        log_fd = open("/tmp/jarvis_restart.log", "a")
        subprocess.Popen(
            ["bash", str(JARVIS_DIR / "bot.sh")],
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            cwd=str(JARVIS_DIR),
            start_new_session=True,
        )
        log_fd.close()  # Popen inherits the fd; close our reference
    except Exception as e:
        msg = f"Failed to start bot.sh: {e}"
        log("ERROR", msg)
        notify_lark("❌ 我发现系统停了，想自己重启但启动失败了，现在还是停的，"
                    "需要你手动看一下。\n\n"
                    f"（启动时的报错原文：{str(e)[:120]}）",
                    incident_key="bot-restart-failed")
        return msg

    last_restart_time = now
    restart_count += 1
    _save_restart_state()

    # Wait for first heartbeat cycle to complete.
    # The first cycle after restart may batch multiple tasks into one Claude call,
    # which can take 60-120s. Wait long enough to avoid false-negative health checks
    # that trigger another restart (creating a restart spiral).
    # Use small increments so we can respond to SIGTERM during this wait.
    for _ in range(90):
        if not running:
            return "shutdown during restart wait"
        time.sleep(1)
    post_check = check_health()
    if post_check["healthy"] and "note" not in post_check:
        msg = f"Auto-restart successful (attempt {restart_count})"
        log("INFO", msg)
        # The log line above keeps its English wording (conversation_audit
        # classifies on "Auto-restart successful"); the card does not.
        notify_lark("✅ 刚才我这边停了，已经自动重启好，现在恢复正常了。\n\n"
                    "停的原因：\n" + _issues_block(issues) + "\n\n知道就行，"
                    "不用你做什么。",
                    incident_key="bot-restart-recovered")
        restart_count = 0
        _save_restart_state()
        # Defensive: a latch created out-of-band must not outlive a verified
        # healthy restart (normally the latch check above prevents reaching
        # this point while latched).
        _clear_breaker_latch("successful restart")
        return msg
    else:
        msg = f"Restart attempt {restart_count} — still unhealthy: {post_check['issues']}"
        log("WARN", msg)
        return msg


def handle_signal(signum, frame):
    global running
    log("INFO", f"Received signal {signum}, shutting down gracefully")
    running = False


def _daemon_pid_is_ours(pid: int) -> bool:
    """Identity check for the singleton pidfile — same recycled-PID class as
    _session_lock_pid_is_ours / bot.sh's BOOT_TS guard (audit 7/10: the same-
    family fixes covered every pidfile EXCEPT the daemon's own). launchd and
    restart.sh both start us as `python3 <JARVIS_DIR>/daemon.py`, so a real
    daemon's argv always carries the repo's absolute daemon.py path — anchored
    full-path match, never a bare substring (7/7 watchdog postmortem rule).
    Fails CLOSED (True) when ps itself errors: wrongly exiting costs one 10s
    launchd throttle cycle; wrongly starting costs a second guardian."""
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                           capture_output=True, text=True, timeout=5)
        args = r.stdout.strip()
    except Exception:
        return True
    if not args:
        return False  # process gone between kill(0) and ps ⇒ stale
    return bool(re.search(
        rf"python[^\s]*\s+.*{re.escape(str(JARVIS_DIR))}/daemon\.py", args))


def acquire_singleton():
    """Ensure only one daemon instance runs. Exit if another is alive.

    os.kill(pid, 0) alone is NOT an identity check (audit 7/10): a forced
    power-off skips release_singleton, the stale pidfile survives the reboot,
    and if the old PID got recycled by a boot process the daemon exited
    'already running' forever — launchd (KeepAlive + ThrottleInterval=10)
    re-ran it into the same wall every 10s, and since the daemon is the only
    thing that starts bot.sh AND the only independent alert channel, the
    whole stack stayed down with zero pages. Alive PIDs must also LOOK like
    a daemon; PermissionError means a foreign (usually root) process — we run
    as a user LaunchAgent, so it can never be us — and is treated as stale
    instead of exiting."""
    if DAEMON_PID_FILE.exists():
        old_pid = None
        try:
            old_pid = int(DAEMON_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # Check if alive
        except (ValueError, ProcessLookupError):
            old_pid = None  # Stale PID file
        except PermissionError:
            print(f"Daemon PID {old_pid} recycled by a foreign process "
                  "(permission denied) — treating pidfile as stale.",
                  file=sys.stderr)
            old_pid = None
        if old_pid is not None:
            if _daemon_pid_is_ours(old_pid):
                print(f"Daemon already running (PID {old_pid}). Exiting.", file=sys.stderr)
                sys.exit(1)
            print(f"Daemon PID {old_pid} is alive but not a daemon.py process "
                  "— treating pidfile as stale.", file=sys.stderr)

    DAEMON_PID_FILE.write_text(str(os.getpid()))


def release_singleton():
    """Remove PID file on exit."""
    try:
        DAEMON_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _ping_external_deadman() -> str:
    """Ping only while process and delivery health are both verified.

    A missed-ping service is useful only if Jarvis stops saying "healthy" when
    the sole user channel has failed. Returning a small status string keeps the
    daemon loop testable without ever exposing the configured endpoint.
    """
    try:
        from core.deadman import ping_due, status

        configured = status(JARVIS_DIR)
        if configured.status == "disabled":
            return "disabled"
        from core.delivery import DeliveryPipeline

        transport = DeliveryPipeline(JARVIS_DIR).transport_health()
        if not transport["healthy"]:
            log(
                "WARN",
                "External dead-man ping withheld: Lark transport has "
                f"{transport['consecutive_failures']} consecutive failures",
            )
            return "withheld_transport_unhealthy"
        result = ping_due(JARVIS_DIR)
        if result.status == "failed":
            log("WARN", f"External dead-man ping failed: {result.detail}")
        return result.status
    except Exception as exc:
        log("WARN", "External dead-man check crashed: "
            f"{type(exc).__name__}")
        return "failed"


def main():
    global running

    acquire_singleton()
    try:
        from core.deploy import register_runtime
        register_runtime("daemon")
    except Exception as exc:
        log("WARN", f"Runtime registration failed: {exc}")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # A hot-reload respawn (REQ-42) must NOT refresh the restart budget
    _load_restart_state()
    # ...nor wipe the component-alert dedup (7/7: 5 respawns in 13min)
    _load_probe_alert_stamps()

    log("INFO", "Guardian daemon started")
    log("INFO", f"  PID: {os.getpid()}")
    log("INFO", f"  JARVIS_DIR: {JARVIS_DIR}")
    log("INFO", f"  USER_ID: {USER_ID[:10]}..." if USER_ID else "  USER_ID: not set")
    log("INFO", f"  Check interval: {CHECK_INTERVAL}s")
    if restart_count:
        log("INFO", f"  Restart budget restored: {restart_count}/{MAX_RESTART_ATTEMPTS} attempts used")

    consecutive_failures = 0
    # Runtime source is immutable for this process. A changed checkout must go
    # through the governed deploy path, which restarts the daemon explicitly;
    # silently hot-loading a branch made unreviewed code enter production.
    probe_tick = 0
    from core.hostclock import Meter

    host_meter = Meter(threshold_s=SLEEP_GAP_THRESHOLD)

    try:
        while running:
            try:
                # Observed-component repair/verify probes every ~4th check
                probe_tick += 1
                if probe_tick % 4 == 0:
                    probe_observed_components()
                    _probe_manifest_criticals()
                    # Delivery dead-letters (backlog #7): retry from an
                    # independent process path.  Lark remains the same channel,
                    # so evidence stays pending when that channel is down.
                    consume_delivery_deadletters()
                # Brain-death repair/verify detection every ~8th check (~4min).
                # Cheaper than a restart-spiral; runs in the daemon because it
                # must survive a dead claude binary that the heartbeat can't.
                if probe_tick % 8 == 0:
                    _check_brain_health()
                    _check_diag_staleness()

                result = check_health()

                if result["healthy"]:
                    if consecutive_failures > 0:
                        log("INFO", f"System recovered after {consecutive_failures} failed checks")
                        consecutive_failures = 0
                    # A genuinely healthy stack re-arms a latched breaker.
                    # Every fake-healthy path (deploy window, active-session
                    # suppression, wake grace) carries a "note" — those must
                    # NOT unlatch (gate logic in _maybe_clear_breaker_latch).
                    _maybe_clear_breaker_latch(result)
                    _maybe_reset_restart_budget(result)
                    # A local watchdog cannot report a powered-off Mac or a
                    # FileVault/pre-login stall. An optional external
                    # missed-ping service can. Ping only from a genuinely
                    # healthy stack; fake-healthy grace paths carry a note and
                    # must not mask the outage externally.
                    if not result.get("note"):
                        _ping_external_deadman()
                else:
                    consecutive_failures += 1
                    log("WARN", f"Health check failed ({consecutive_failures}x): {result['issues']}")

                    if consecutive_failures >= 2:
                        fix_result = diagnose_and_fix(result["issues"])
                        log("INFO", f"Fix result: {fix_result}")
                        # Reset consecutive counter after taking action
                        # (give the restart time to take effect)
                        consecutive_failures = 0

            except Exception as e:
                log("ERROR", f"Health check exception: {e}")

            # Sleep in small increments so we can respond to signals. If the
            # machine slept, the loop resumes much later; record a wake grace
            # before the next health check so stale heartbeat age does not
            # trigger a false restart while the stack catches up.
            sleep_started = time.time()
            for _ in range(CHECK_INTERVAL):
                if not running:
                    break
                time.sleep(1)
            if running:
                # Two ways the loop loses time: our own naps overshot (any
                # stall, including CPU starvation) or the host slept somewhere
                # else in the tick — inside the health check itself, which the
                # nap measurement cannot see. Take the larger.
                overshoot = time.time() - sleep_started - CHECK_INTERVAL
                lost = max(overshoot, host_meter.gap())
                _observe_absence(
                    _record_wake_gap(CHECK_INTERVAL + lost, CHECK_INTERVAL))
    finally:
        release_singleton()
        log("INFO", "Guardian daemon stopped")


if __name__ == "__main__":
    main()
