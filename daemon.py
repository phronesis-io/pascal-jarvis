#!/usr/bin/env python3
"""Jarvis Guardian Daemon — keeps the system alive and healthy.

A persistent background process that:
1. Periodically checks Jarvis health (bot.sh, heartbeat, Lark listener)
2. On failure: notifies Pascal via Lark + uses Claude to diagnose + auto-restarts
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

# ── Config ──
JARVIS_DIR = Path(__file__).parent
CHECK_INTERVAL = 120          # seconds between health checks (2 min)
HEARTBEAT_STALE_THRESHOLD = 900  # 15 min without heartbeat = stale (tasks can take a while)
MAX_RESTART_ATTEMPTS = 3
RESTART_COOLDOWN = 300        # 5 min between restart attempts
LOG_FILE = JARVIS_DIR / "daemon.log"
MAX_LOG_LINES = 1000

# Lark config (read from jarvis.yaml)
USER_ID = ""
try:
    import yaml
    cfg = yaml.safe_load((JARVIS_DIR / "jarvis.yaml").read_text())
    USER_ID = cfg.get("lark", {}).get("user_id", "")
except Exception:
    # Fallback: parse yaml manually for user_id
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
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
        # Rotate if too large
        if LOG_FILE.stat().st_size > 200_000:
            lines = LOG_FILE.read_text().splitlines()
            LOG_FILE.write_text("\n".join(lines[-500:]) + "\n")
    except Exception:
        pass


def notify_lark(msg: str):
    """Send a notification to Pascal via Lark. Uses lark-cli directly."""
    if not USER_ID:
        log("WARN", "No USER_ID configured, cannot notify Lark")
        return
    try:
        subprocess.run(
            ["lark-cli", "im", "+messages-send",
             "--user-id", USER_ID,
             "--markdown", f"🛡️ **Guardian Daemon**\n\n{msg}",
             "--as", "bot"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        log("ERROR", f"Lark notify failed: {e}")


def check_health() -> dict:
    """Run health checks. Returns {"healthy": bool, "issues": [str]}."""
    issues = []

    # 1. Is bot.sh running?
    try:
        r = subprocess.run(["pgrep", "-f", "bash.*bot\\.sh"],
                           capture_output=True, text=True, timeout=5)
        if not r.stdout.strip():
            issues.append("bot.sh is not running")
    except Exception as e:
        issues.append(f"Cannot check bot.sh: {e}")

    # 2. Is Lark listener connected?
    try:
        r = subprocess.run(["pgrep", "-f", "lark-cli event"],
                           capture_output=True, text=True, timeout=5)
        if not r.stdout.strip():
            issues.append("Lark event listener is not running")
    except Exception as e:
        issues.append(f"Cannot check Lark listener: {e}")

    # 3. Is heartbeat alive? (check log for recent beat)
    heartbeat_log = Path("/tmp/jarvis_restart.log")
    if heartbeat_log.exists():
        try:
            text = heartbeat_log.read_text(errors="ignore")
            beats = re.findall(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*heartbeat.*Beat sent", text)
            if beats:
                last_beat_str = beats[-1]
                last_beat = datetime.strptime(last_beat_str, "%Y-%m-%d %H:%M:%S")
                age = (datetime.now() - last_beat).total_seconds()
                if age > HEARTBEAT_STALE_THRESHOLD:
                    issues.append(f"Heartbeat stale ({int(age)}s since last beat)")
            # Also check for "Starting" if no beats yet
            starts = re.findall(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*heartbeat.*Starting", text)
            if starts and not beats:
                last_start_str = starts[-1]
                last_start = datetime.strptime(last_start_str, "%Y-%m-%d %H:%M:%S")
                age = (datetime.now() - last_start).total_seconds()
                if age > HEARTBEAT_STALE_THRESHOLD:
                    issues.append(f"Heartbeat started but no beats in {int(age)}s")
        except Exception as e:
            issues.append(f"Cannot parse heartbeat log: {e}")
    else:
        issues.append("No heartbeat log found")

    # 4. Recent fatal errors?
    jarvis_log = JARVIS_DIR / "jarvis.log"
    if jarvis_log.exists():
        try:
            tail = jarvis_log.read_text(errors="ignore")[-5000:]
            fatals = re.findall(r"(FATAL|panic|Traceback|unbound variable).*", tail, re.IGNORECASE)
            if fatals:
                issues.append(f"Recent errors in jarvis.log: {fatals[-1][:100]}")
        except Exception:
            pass

    return {"healthy": len(issues) == 0, "issues": issues}


def diagnose_and_fix(issues: list[str]) -> str:
    """Use Claude to diagnose the problem and attempt a fix."""
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
        restart_count = 0  # Reset after notifying
        last_restart_time = now + 600  # Extra cooldown
        return msg

    log("INFO", f"Attempting fix for: {issues}")

    # Collect diagnostics
    diag_parts = [f"Issues: {issues}"]
    try:
        r = subprocess.run(["tail", "-30", "/tmp/jarvis_restart.log"],
                           capture_output=True, text=True, timeout=5)
        diag_parts.append(f"Recent log:\n{r.stdout[-2000:]}")
    except Exception:
        pass

    # Kill everything: bot, lark, admin, AND any stuck claude processes from jarvis
    log("INFO", "Killing existing processes...")
    for pattern in ["lark-cli event", "bash.*bot\\.sh", "admin\\.py"]:
        subprocess.run(["pkill", "-f", pattern],
                       capture_output=True, timeout=5)

    # Kill stuck claude processes spawned by bot.sh (the --dangerously-skip-permissions ones)
    try:
        r = subprocess.run(["pgrep", "-f", "claude.*dangerously-skip"],
                           capture_output=True, text=True, timeout=5)
        for pid in r.stdout.strip().split("\n"):
            if pid.strip():
                subprocess.run(["kill", pid.strip()], capture_output=True, timeout=5)
                log("INFO", f"Killed stuck claude process: {pid.strip()}")
    except Exception:
        pass

    # Clean stale session locks
    import glob
    for lock in glob.glob(str(JARVIS_DIR / ".session_lock_*")):
        try:
            os.remove(lock)
            log("INFO", f"Removed stale lock: {lock}")
        except Exception:
            pass

    time.sleep(3)

    log("INFO", "Starting bot.sh...")
    try:
        subprocess.Popen(
            ["bash", str(JARVIS_DIR / "bot.sh")],
            stdout=open("/tmp/jarvis_restart.log", "a"),
            stderr=subprocess.STDOUT,
            cwd=str(JARVIS_DIR),
            start_new_session=True,  # Detach from daemon
        )
    except Exception as e:
        msg = f"Failed to start bot.sh: {e}"
        log("ERROR", msg)
        notify_lark(f"❌ {msg}")
        return msg

    last_restart_time = now
    restart_count += 1

    # Wait and verify
    time.sleep(8)
    post_check = check_health()
    if post_check["healthy"]:
        msg = f"Auto-restart successful (attempt {restart_count})"
        log("INFO", msg)
        notify_lark(f"✅ Jarvis was down. {msg}.\n\nOriginal issues:\n" +
                    "\n".join(f"- {i}" for i in issues))
        restart_count = 0  # Reset on success
        return msg
    else:
        msg = f"Restart attempt {restart_count} — still unhealthy: {post_check['issues']}"
        log("WARN", msg)
        return msg


def handle_signal(signum, frame):
    global running
    log("INFO", f"Received signal {signum}, shutting down gracefully")
    running = False


def main():
    global running

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log("INFO", "Guardian daemon started")
    log("INFO", f"  JARVIS_DIR: {JARVIS_DIR}")
    log("INFO", f"  USER_ID: {USER_ID[:10]}..." if USER_ID else "  USER_ID: not set")
    log("INFO", f"  Check interval: {CHECK_INTERVAL}s")

    consecutive_failures = 0

    while running:
        try:
            result = check_health()

            if result["healthy"]:
                if consecutive_failures > 0:
                    log("INFO", f"System recovered after {consecutive_failures} failed checks")
                    consecutive_failures = 0
                # Silent — healthy is the default state
            else:
                consecutive_failures += 1
                log("WARN", f"Health check failed ({consecutive_failures}x): {result['issues']}")

                if consecutive_failures >= 2:
                    # Two consecutive failures → take action
                    fix_result = diagnose_and_fix(result["issues"])
                    log("INFO", f"Fix result: {fix_result}")

        except Exception as e:
            log("ERROR", f"Health check exception: {e}")

        # Sleep in small increments so we can respond to signals
        for _ in range(CHECK_INTERVAL):
            if not running:
                break
            time.sleep(1)

    log("INFO", "Guardian daemon stopped")


if __name__ == "__main__":
    main()
