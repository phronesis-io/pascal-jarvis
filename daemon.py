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
MAX_RESTART_ATTEMPTS = 3
RESTART_COOLDOWN = 300        # 5 min between restart attempts
LOG_FILE = JARVIS_DIR / "daemon.log"
DAEMON_PID_FILE = JARVIS_DIR / ".daemon.pid"
BOT_PID_FILE = JARVIS_DIR / ".bot.pid"
MAX_LOG_LINES = 1000

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


def log(level: str, msg: str):
    """Append to daemon.log with rotation."""
    ts = _daemon_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
        if LOG_FILE.stat().st_size > 200_000:
            lines = LOG_FILE.read_text().splitlines()
            LOG_FILE.write_text("\n".join(lines[-500:]) + "\n")
    except Exception:
        pass


def notify_lark(msg: str):
    """Send a notification to Pascal — Lark first, local banner fallback.

    REQ-58: every alert path used to flow through the same lark-cli + app
    credentials as the system being alerted about — a Lark/auth outage took
    down both the system AND every alarm about it ('如果你看到这条说明链路已
    部分恢复'). On Lark failure: macOS banner (Pascal is usually at this Mac)
    + a dead-letter line flushed on the next successful send.
    """
    if not USER_ID:
        log("WARN", "No USER_ID configured, cannot notify Lark")
        return
    ok = False
    try:
        r = subprocess.run(
            ["lark-cli", "im", "+messages-send",
             "--user-id", USER_ID,
             "--markdown", f"🛡️ **Guardian Daemon**\n\n{msg}",
             "--as", "bot"],
            capture_output=True, text=True, timeout=15,
        )
        ok = r.returncode == 0
    except Exception as e:
        log("ERROR", f"Lark notify failed: {e}")

    dead_letter = JARVIS_DIR / "alerts_deadletter.jsonl"
    if ok:
        # Flush ALL dead letters from earlier outages, and only delete the
        # file if the re-send actually succeeded (red-team fix: the old code
        # took only the last 10 — silently dropping older alerts — and
        # unlinked unconditionally, losing everything if the flush itself
        # failed).
        if dead_letter.exists():
            try:
                lines = [l for l in dead_letter.read_text().splitlines() if l.strip()]
                pending = []
                for l in lines:
                    try:
                        pending.append(json.loads(l).get("msg", ""))
                    except (json.JSONDecodeError, ValueError):
                        continue
                if pending:
                    # Chunk so a huge backlog can't exceed message limits;
                    # only unlink if every chunk sent.
                    all_sent = True
                    for i in range(0, len(pending), 10):
                        chunk = pending[i:i + 10]
                        r2 = subprocess.run(
                            ["lark-cli", "im", "+messages-send", "--user-id", USER_ID,
                             "--markdown", "🛡️ **Guardian Daemon**（补发告警 — 当时 Lark 链路不通）\n\n"
                             + "\n---\n".join(chunk), "--as", "bot"],
                            capture_output=True, text=True, timeout=15)
                        if r2.returncode != 0:
                            all_sent = False
                            break
                    if all_sent:
                        dead_letter.unlink(missing_ok=True)
                else:
                    dead_letter.unlink(missing_ok=True)  # only unparseable junk
            except Exception:
                pass  # leave the file for the next successful notify
        return

    # Lark failed → local banner + dead letter
    try:
        safe = msg.replace('"', "'")[:200]
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "Jarvis Guardian (Lark链路不通)"'],
            capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        with open(dead_letter, "a") as f:
            f.write(json.dumps({"ts": _daemon_now().strftime("%Y-%m-%d %H:%M:%S"),
                                 "msg": msg}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _find_last_heartbeat() -> float | None:
    """Find the most recent 'Beat sent' timestamp from any log file.
    Returns age in seconds, or None if not found."""
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
            # Only read last 10KB to avoid memory issues on large logs
            size = log_path.stat().st_size
            with open(log_path, "r", errors="ignore") as f:
                if size > 10_000:
                    f.seek(size - 10_000)
                    f.readline()  # skip partial line
                text = f.read()
            # Match both old format [YYYY-MM-DD HH:MM:SS]...Beat sent
            # and new JSON format {"ts":"YYYY-MM-DDTHH:MM:SS",...,"msg":"Beat sent"...}
            beats = re.findall(
                r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*heartbeat.*Beat sent",
                text
            )
            # Also check structured JSON logs
            json_beats = re.findall(
                r'"ts":"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})".*"component":"heartbeat"',
                text
            )
            for tb in json_beats:
                beats.append(tb.replace("T", " "))
            if beats:
                beat_time = datetime.strptime(beats[-1], "%Y-%m-%d %H:%M:%S")
                if latest_beat is None or beat_time > latest_beat:
                    latest_beat = beat_time
        except Exception:
            continue

    if latest_beat:
        return (_daemon_now().replace(tzinfo=None) - latest_beat).total_seconds()
    return None


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
    beat_age = _find_last_heartbeat()
    if beat_age is None:
        issues.append("No heartbeat found in any log file")
    elif beat_age > HEARTBEAT_STALE_THRESHOLD:
        # Check if a user message is being processed (session lock exists).
        # Long Claude calls for user conversations (5-10 min) block heartbeat.
        # Restarting during an active conversation KILLS the user's response.
        import glob as _glob
        active_locks = _glob.glob(str(JARVIS_DIR / ".session_lock_*"))
        if active_locks:
            log("INFO", f"Heartbeat stale ({int(beat_age)}s) but {len(active_locks)} "
                f"session(s) active — NOT restarting (would kill user's response)")
        elif _in_wake_grace():
            log("INFO", f"Heartbeat stale ({int(beat_age)}s) but host just woke — "
                f"grace {WAKE_GRACE_SECONDS}s, NOT restarting")
        else:
            issues.append(f"Heartbeat stale ({int(beat_age)}s since last beat)")

    # 4. Fatal error detection REMOVED.
    # The old regex matched "Traceback" anywhere in the log tail, including
    # inside structured JSON log messages that merely REPORT script errors.
    # This caused false restarts (e.g. eigenflux_profile_post.py traceback
    # logged by heartbeat._log → daemon saw "Traceback" → restart).
    # The bot/heartbeat/lark health checks above are sufficient.
    # Script-level errors are handled by circuit breaker, not daemon restarts.

    return {"healthy": len(issues) == 0, "issues": issues}


# Components the daemon observes but does NOT own (REQ-40): :3456 admin and
# :3457 dashboard belong to bot.sh's watchdog and launchd respectively. The
# daemon's job here is ALERT-ONLY — it must never start new fights (the 6/12
# restart spiral lesson). One Lark line per component per 4h.
_probe_alert_stamps: dict = {}
PROBE_ALERT_WINDOW = 4 * 3600

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


def probe_observed_components():
    """Alert (never restart) when :3456/:3457 are down — the dashboard died
    for 23 days because nothing watched it. Errors here never raise."""
    # During a restart window the stack is legitimately down — probing here
    # would fire false 'component DOWN' alerts (red-team fix: the probe ran
    # OUTSIDE check_health's deploy guard).
    if _in_deploy_window():
        return
    import urllib.request
    import urllib.error
    for name, url in (("admin :3456", "http://127.0.0.1:3456/health"),
                      ("dashboard :3457", "http://127.0.0.1:3457/")):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    _probe_alert_stamps.pop(name, None)  # recovered
                    continue
        except urllib.error.HTTPError:
            # The server RESPONDED (e.g. /health 503 "degraded" because a
            # circuit is open). It is alive, not 失联 — never page "探测不通"
            # for this. Connection-level failures fall through to the alert.
            _probe_alert_stamps.pop(name, None)
            continue
        except Exception:
            pass
        last = _probe_alert_stamps.get(name, 0)
        if time.time() - last >= PROBE_ALERT_WINDOW:
            _probe_alert_stamps[name] = time.time()
            log("WARN", f"Observed component DOWN: {name}")
            notify_lark(f"⚠️ 组件失联：{name} 探测不通。"
                        f"（守护进程只告警不代管；如未自愈请重启或查 launchd）")


def _check_brain_health():
    """Alert (never restart) when the heartbeat loop is ALIVE BUT BRAIN-DEAD —
    ticking every cycle while every claude_call fails. On 2026-06-15 `claude`
    was missing from the launchd PATH for ~1h and EVERY liveness signal stayed
    fresh (beat-marker, /health heartbeat_age, per-task circuit), so nothing
    caught it. The daemon is Claude-independent and can: it reads
    heartbeat_state.json directly and applies the 'ran-but-failing' detectors in
    core/brain_health.py. Alert-only, 4h dedup, deploy-guarded. Never raises."""
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

        # Post-sleep/reboot grace: a stale last_success right after the host
        # was asleep or powered off is expected, not brain-death. Two gap
        # signals — the in-process wake mark (laptop lid), and a hole in our
        # own check cadence (reboot wipes last_wake_time; this file survives).
        now = time.time()
        if last_wake_time:
            grace_until = max(grace_until, last_wake_time + BRAIN_WAKE_GRACE)
        if last_check_ts and now - last_check_ts > BRAIN_CHECK_GAP_THRESHOLD:
            grace_until = max(grace_until, now + BRAIN_WAKE_GRACE)
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

        new_last_alert = last_alert
        if result["brain_dead"] and in_grace:
            log("INFO", "brain-health: would alert but in post-wake grace: "
                + "; ".join(result["alerts"]))
        elif result["brain_dead"] and now - last_alert >= PROBE_ALERT_WINDOW:
            new_last_alert = now
            log("WARN", "BRAIN-DEAD heartbeat: " + "; ".join(result["alerts"]))
            notify_lark(result["summary"])

        # Atomic persist: samples carry the per-priority failure windows across
        # hot-reloads; last_alert enforces the 4h dedup; last_check_ts/grace_until
        # drive the sleep-gap detection above.
        new_state = {"samples": result["samples"], "last_alert": new_last_alert,
                     "last_check_ts": now, "grace_until": grace_until}
        tmp = BRAIN_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_state))
        os.replace(tmp, BRAIN_STATE_FILE)
    except Exception as e:
        log("ERROR", f"brain-health check failed: {e}")


def diagnose_and_fix(issues: list[str]) -> str:
    """Kill existing processes and restart bot.sh."""
    global last_restart_time, restart_count

    now = time.time()

    # Cooldown check
    if now - last_restart_time < RESTART_COOLDOWN:
        remaining = int(RESTART_COOLDOWN - (now - last_restart_time))
        log("INFO", f"Restart cooldown active ({remaining}s remaining)")
        return f"restart cooldown ({remaining}s remaining)"

    if restart_count >= MAX_RESTART_ATTEMPTS:
        msg = f"Reached max restart attempts ({MAX_RESTART_ATTEMPTS}). Manual intervention needed."
        log("ERROR", msg)
        notify_lark(f"⚠️ {msg}\n\nIssues:\n" + "\n".join(f"- {i}" for i in issues))
        restart_count = 0
        last_restart_time = now + 600  # 10min extra cooldown
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
    # (lock format: "<pid> <token>" — first field is the pid)
    import glob as _glob
    for lock in _glob.glob(str(JARVIS_DIR / ".session_lock_*")):
        try:
            content = Path(lock).read_text().strip()
            pid = content.split()[0] if content else ""
            if pid.isdigit():
                subprocess.run(["kill", pid], capture_output=True, timeout=5)
                log("INFO", f"Killed stuck claude process from lock: {pid}")
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
        notify_lark(f"❌ {msg}")
        return msg

    last_restart_time = now
    restart_count += 1

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
    if post_check["healthy"]:
        msg = f"Auto-restart successful (attempt {restart_count})"
        log("INFO", msg)
        notify_lark(f"✅ Jarvis was down. {msg}.\n\nOriginal issues:\n" +
                    "\n".join(f"- {i}" for i in issues))
        restart_count = 0
        return msg
    else:
        msg = f"Restart attempt {restart_count} — still unhealthy: {post_check['issues']}"
        log("WARN", msg)
        return msg


def handle_signal(signum, frame):
    global running
    log("INFO", f"Received signal {signum}, shutting down gracefully")
    running = False


def acquire_singleton():
    """Ensure only one daemon instance runs. Exit if another is alive."""
    if DAEMON_PID_FILE.exists():
        try:
            old_pid = int(DAEMON_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # Check if alive
            print(f"Daemon already running (PID {old_pid}). Exiting.", file=sys.stderr)
            sys.exit(1)
        except (ValueError, ProcessLookupError):
            pass  # Stale PID file
        except PermissionError:
            print(f"Daemon PID {old_pid} exists but permission denied. Exiting.", file=sys.stderr)
            sys.exit(1)

    DAEMON_PID_FILE.write_text(str(os.getpid()))


def release_singleton():
    """Remove PID file on exit."""
    try:
        DAEMON_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    global running

    acquire_singleton()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log("INFO", "Guardian daemon started")
    log("INFO", f"  PID: {os.getpid()}")
    log("INFO", f"  JARVIS_DIR: {JARVIS_DIR}")
    log("INFO", f"  USER_ID: {USER_ID[:10]}..." if USER_ID else "  USER_ID: not set")
    log("INFO", f"  Check interval: {CHECK_INTERVAL}s")

    consecutive_failures = 0
    # Stale-code hot reload (REQ-42): the long-lived daemon never noticed its
    # on-disk code changed — on 6/12 a pre-deploy daemon killed a healthy bot
    # twice by enforcing outdated rules. When disk is newer, exit 0 and let
    # launchd KeepAlive respawn us on fresh code within seconds.
    _code_mtime = os.path.getmtime(__file__)
    probe_tick = 0

    try:
        while running:
            try:
                try:
                    if os.path.getmtime(__file__) > _code_mtime + 1:
                        log("INFO", "daemon.py changed on disk — exiting for "
                            "launchd respawn (hot reload)")
                        break
                except OSError:
                    pass

                # Observed-component probes (alert-only) every ~4th check
                probe_tick += 1
                if probe_tick % 4 == 0:
                    probe_observed_components()
                # Brain-death detection (alert-only) every ~8th check (~4min).
                # Cheaper than a restart-spiral; runs in the daemon because it
                # must survive a dead claude binary that the heartbeat can't.
                if probe_tick % 8 == 0:
                    _check_brain_health()

                result = check_health()

                if result["healthy"]:
                    if consecutive_failures > 0:
                        log("INFO", f"System recovered after {consecutive_failures} failed checks")
                        consecutive_failures = 0
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
                _record_wake_gap(time.time() - sleep_started, CHECK_INTERVAL)
    finally:
        release_singleton()
        log("INFO", "Guardian daemon stopped")


if __name__ == "__main__":
    main()
