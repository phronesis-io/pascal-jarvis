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


def _is_bot_alive() -> bool:
    """Check if bot.sh is alive via PID file (primary) or pgrep (fallback)."""
    # Primary: check PID file (format: "PID" or "PID BOOT_TS")
    if BOT_PID_FILE.exists():
        try:
            pid = int(BOT_PID_FILE.read_text().strip().split()[0])
            os.kill(pid, 0)  # Check if process exists
            return True
        except (ValueError, ProcessLookupError, PermissionError, IndexError):
            pass

    # Fallback: pgrep, anchored to this repo's bot.sh — a wide pattern would
    # see another project's bot.sh as "healthy" while ours is down.
    import re as _re
    try:
        r = subprocess.run(["pgrep", "-f", f"bash.*{_re.escape(str(JARVIS_DIR))}/bot\\.sh"],
                           capture_output=True, text=True, timeout=5)
        return bool(r.stdout.strip())
    except Exception:
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
    if not _is_bot_alive():
        issues.append("bot.sh is not running")

    # 2. Is Lark listener connected?
    try:
        # Either event backend counts as the listener (lark-cli or the
        # single-connection python sidecar)
        r = subprocess.run(["pgrep", "-f", "lark-cli event|lark_event_sidecar"],
                           capture_output=True, text=True, timeout=5)
        if not r.stdout.strip():
            issues.append("Lark event listener is not running")
    except Exception as e:
        issues.append(f"Cannot check Lark listener: {e}")

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
    for name, url in (("admin :3456", "http://127.0.0.1:3456/health"),
                      ("dashboard :3457", "http://127.0.0.1:3457/")):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    _probe_alert_stamps.pop(name, None)  # recovered
                    continue
        except Exception:
            pass
        last = _probe_alert_stamps.get(name, 0)
        if time.time() - last >= PROBE_ALERT_WINDOW:
            _probe_alert_stamps[name] = time.time()
            log("WARN", f"Observed component DOWN: {name}")
            notify_lark(f"⚠️ 组件失联：{name} 探测不通。"
                        f"（守护进程只告警不代管；如未自愈请重启或查 launchd）")


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

            # Sleep in small increments so we can respond to signals
            for _ in range(CHECK_INTERVAL):
                if not running:
                    break
                time.sleep(1)
    finally:
        release_singleton()
        log("INFO", "Guardian daemon stopped")


if __name__ == "__main__":
    main()
