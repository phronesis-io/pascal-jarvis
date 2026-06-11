#!/usr/bin/env python3
"""Heartbeat loop — the main cycle that drives Jarvis.

Replaces the bash heartbeat_loop() function in bot.sh. All logic that was
in bash (output routing, card/text splitting, outbox writing, engagement
tracking, restart trigger detection) is now in Python where it can be tested.

bot.sh launches this as: python3 -m core.heartbeat_loop &

The loop:
  1. Check for restart trigger
  2. Check for force trigger
  3. Run HeartbeatRunner.run_cycle() → get output
  4. Route output: cards → lark_send_card, text → lark_send
  5. Write to outbox + engagement log
  6. Sleep CHECK_INTERVAL seconds
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from core.card import extract_card_text, extract_readable_from_output, linkify_bare_urls
from core.heartbeat import HeartbeatRunner
from core.log import log
from core.safety import looks_like_error
from core.timeutil import now_local_str


def _lark_send_card(card_json: str, user_id: str, log_file: str) -> bool:
    """Send a Lark interactive card, with retries. Returns True on success."""
    if not user_id:
        return False
    for attempt, delay in enumerate((0,) + SEND_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            r = subprocess.run(
                ["lark-cli", "im", "+messages-send",
                 "--user-id", user_id, "--msg-type", "interactive",
                 "--content", card_json, "--as", "bot"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                if attempt:
                    log("heartbeat", f"Card send succeeded on retry {attempt}")
                return True
        except Exception as e:
            log("heartbeat", f"lark_send_card attempt {attempt} failed: {e}", level="warn")
    return False


# Retry sleeps between send attempts (REQ-11). Transient lark-cli/network
# hiccups were surfacing as silent message loss the user discovered by hand.
SEND_RETRY_DELAYS = (2, 5)


def _lark_send_text(text: str, user_id: str) -> bool:
    """Send plain text to Lark, with retries on transient failure."""
    if not user_id or not text:
        return False
    text = linkify_bare_urls(text)
    for attempt, delay in enumerate((0,) + SEND_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            r = subprocess.run(
                ["lark-cli", "im", "+messages-send",
                 "--user-id", user_id, "--markdown", text, "--as", "bot"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                if attempt:
                    log("heartbeat", f"Text send succeeded on retry {attempt}")
                return True
        except Exception:
            pass
    return False


def _route_output(output: str, user_id: str, jarvis_dir: Path) -> bool:
    """Route heartbeat output to Lark: cards via card API, text via markdown.

    Returns True if every part was delivered (used by the delivery ledger).
    """
    remaining_text_parts = []
    results = []

    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.startswith("CARD:"):
            card_json = line[5:]
            if _lark_send_card(card_json, user_id, ""):
                results.append(True)
            else:
                # Fallback to text
                text = extract_card_text(card_json)
                if text:
                    results.append(_lark_send_text(text, user_id))
                else:
                    log("heartbeat", "Card send + text extraction both failed", level="warn")
                    results.append(False)

        elif line.startswith('{"config":'):
            # Legacy card format
            results.append(_lark_send_card(line, user_id, ""))

        else:
            # Block raw JSON from reaching user
            try:
                json.loads(line)
                log("heartbeat", f"Blocked raw JSON: {line[:100]}...", level="warn")
                continue
            except (json.JSONDecodeError, ValueError):
                pass
            remaining_text_parts.append(line)

    if remaining_text_parts:
        results.append(_lark_send_text("\n".join(remaining_text_parts), user_id))

    return all(results) if results else True


# ── Delivery ledger + aggregate alert (REQ-11) ──────────────────────
# Past failures were invisible: the bot generated a reply, the send died, and
# the user found out by sending "hi" to probe for life. Track consecutive
# failures and tell the user ONCE, instead of going silent.

DELIVERY_STATE_FILE = ".delivery_state.json"
DELIVERY_ALERT_THRESHOLD = 3
DELIVERY_ALERT_COOLDOWN = 2 * 3600


def _note_delivery(jarvis_dir: Path, ok: bool, user_id: str = "",
                   now: float | None = None):
    """Track consecutive delivery failures; alert the user past the threshold."""
    now = now if now is not None else time.time()
    state_path = jarvis_dir / DELIVERY_STATE_FILE
    try:
        st = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        st = {}

    if ok:
        if st.get("consec_fails", 0) >= DELIVERY_ALERT_THRESHOLD:
            log("heartbeat", f"Delivery recovered after {st['consec_fails']} failures")
        st["consec_fails"] = 0
    else:
        st["consec_fails"] = st.get("consec_fails", 0) + 1
        log("heartbeat", f"Delivery failure #{st['consec_fails']}", level="warn")
        if (st["consec_fails"] >= DELIVERY_ALERT_THRESHOLD
                and now - st.get("last_alert", 0) > DELIVERY_ALERT_COOLDOWN):
            alert = (f"⚠️ 最近 {st['consec_fails']} 条消息可能没有送达"
                     "（发送链路在重试后仍失败）。我会继续重试；"
                     "如果你看到这条说明链路已部分恢复。")
            if _lark_send_text(alert, user_id):
                st["last_alert"] = now

    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, state_path)


# ── Quiet hours queue (REQ-13) ───────────────────────────────────────
# 0:00–9:00 sends had a ≤6% reply rate vs 70%+ in daytime windows. Non-urgent
# proactive messages generated at night are queued and delivered as one
# digest after the window opens. Urgent sources bypass the queue.

QUIET_START_MIN = 23 * 60 + 30   # 23:30
QUIET_END_MIN = 9 * 60 + 30      # 09:30
URGENT_SOURCES = {"intention-check", "calendar-sync", "checkin"}
NIGHT_QUEUE_FILE = "night_queue.jsonl"
NIGHT_QUEUE_MAX = 20


def _in_quiet_hours(minutes_of_day: int | None = None) -> bool:
    if minutes_of_day is None:
        t = time.localtime()
        minutes_of_day = t.tm_hour * 60 + t.tm_min
    return minutes_of_day >= QUIET_START_MIN or minutes_of_day < QUIET_END_MIN


def _peek_source(jarvis_dir: Path) -> str:
    """Read the source sidecar WITHOUT consuming it (that's _record_engagement's job)."""
    try:
        return (jarvis_dir / ".heartbeat_last_source").read_text().strip()
    except OSError:
        return ""


def _is_urgent(source_str: str) -> bool:
    """True if any of the (comma-separated) sources is an urgent one."""
    return bool(URGENT_SOURCES & {s.strip() for s in source_str.split(",") if s.strip()})


def _queue_for_morning(output: str, jarvis_dir: Path):
    readable = extract_readable_from_output(output) or output
    source = _peek_source(jarvis_dir)
    (jarvis_dir / ".heartbeat_last_source").unlink(missing_ok=True)
    entry = {"ts": now_local_str("%Y-%m-%d %H:%M"),
             "text": readable, "source": source or "heartbeat"}
    with open(jarvis_dir / NIGHT_QUEUE_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log("heartbeat", f"Queued for morning (quiet hours, source={source or 'heartbeat'})")


def _flush_night_queue(jarvis_dir: Path, user_id: str) -> bool:
    """Send queued night messages as one digest. Returns True if flushed."""
    queue_path = jarvis_dir / NIGHT_QUEUE_FILE
    if not queue_path.exists():
        return False
    entries = []
    for line in queue_path.read_text().splitlines()[-NIGHT_QUEUE_MAX:]:
        try:
            entries.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    if not entries:
        queue_path.unlink(missing_ok=True)
        return False

    parts = [f"🌙 **夜间积累的 {len(entries)} 条消息**"]
    for e in entries:
        parts.append(f"\n— {e.get('ts', '')} · {e.get('source', '')} —\n{e.get('text', '')}")
    digest = "\n".join(parts)

    if _lark_send_text(digest, user_id):
        queue_path.unlink(missing_ok=True)
        _write_outbox(digest, jarvis_dir)
        log("heartbeat", f"Flushed night queue ({len(entries)} entries)")
        return True
    log("heartbeat", "Night queue flush failed — will retry next cycle", level="warn")
    return False


DEDUP_WINDOW_SECONDS = 6 * 3600


def _is_duplicate_send(output: str, jarvis_dir: Path) -> bool:
    """True if an identical message already went out within the dedup window.

    Compares against recent heartbeat_outbox.jsonl entries. Guards against
    repeat spam: the same error card went out 7 times in 12 hours on
    2026-06-10, and users have reported duplicate checkins before. Content
    must match exactly — legitimate messages are timestamped/contextual and
    practically never repeat verbatim.
    """
    outbox = jarvis_dir / "heartbeat_outbox.jsonl"
    if not outbox.exists():
        return False
    readable = extract_readable_from_output(output) or output
    now = time.time()
    try:
        lines = outbox.read_text().splitlines()[-30:]
    except OSError:
        return False
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get("text") != readable:
            continue
        try:
            sent = time.mktime(time.strptime(entry.get("ts", ""), "%Y-%m-%d %H:%M"))
        except (ValueError, OverflowError):
            continue
        if now - sent < DEDUP_WINDOW_SECONDS:
            return True
    return False


def _write_outbox(output: str, jarvis_dir: Path):
    """Write heartbeat output to outbox for main session visibility."""
    ts = now_local_str("%Y-%m-%d %H:%M")
    readable = extract_readable_from_output(output) or output
    try:
        text_json = json.dumps(readable, ensure_ascii=False)
    except (TypeError, ValueError):
        text_json = json.dumps(str(readable))
    entry = f'{{"role":"assistant","text":{text_json},"ts":"{ts}","source":"heartbeat"}}\n'
    with open(jarvis_dir / "heartbeat_outbox.jsonl", "a") as f:
        f.write(entry)


def _record_engagement(jarvis_dir: Path):
    """Record heartbeat send in engagement log using source sidecar file."""
    ts = now_local_str("%Y-%m-%d %H:%M")
    epoch = int(time.time())
    source_file = jarvis_dir / ".heartbeat_last_source"
    if source_file.exists():
        sources = source_file.read_text().strip()
        source_file.unlink(missing_ok=True)
    else:
        sources = "heartbeat"

    elog = jarvis_dir / "engagement_log.jsonl"
    with open(elog, "a") as f:
        for src in sources.split(","):
            src = src.strip()
            if src:
                entry = {"ts": ts, "source": src, "type": "sent", "epoch": epoch}
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _trim_file(path: Path, max_lines: int):
    """Trim a JSONL file to last N lines."""
    if not path.exists():
        return
    lines = path.read_text().splitlines()
    if len(lines) > max_lines:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines[-max_lines:]) + "\n")
        os.replace(tmp, path)


def run_loop(jarvis_dir: str, memory_dir: str, model: str = "opus",
             work_dir: str = "", check_interval: int = 10, user_id: str = "",
             claude_timeout: int = 600):
    """Main heartbeat loop. Runs forever until killed."""
    jd = Path(jarvis_dir)
    heartbeat_trigger = Path("/tmp/jarvis-heartbeat-trigger")

    # Trim logs on startup
    _trim_file(jd / "heartbeat_outbox.jsonl", 20)
    _trim_file(jd / "engagement_log.jsonl", 500)

    runner = HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=str(jd / "HEARTBEAT.md"),
        state_file=str(jd / "heartbeat_state.json"),
        memory_dir=memory_dir,
        model=model,
        work_dir=work_dir or jarvis_dir,
        claude_timeout=claude_timeout,
    )

    log("heartbeat", f"Starting ({check_interval}s cycle)")

    while True:
        # Check restart trigger
        restart_trigger = jd / ".restart_trigger"
        if restart_trigger.exists():
            restart_trigger.unlink(missing_ok=True)
            log("heartbeat", "Restart trigger detected — exiting")
            os.kill(0, signal.SIGTERM)
            break

        # Check force trigger
        force = False
        if heartbeat_trigger.exists():
            heartbeat_trigger.unlink(missing_ok=True)
            force = True
            log("heartbeat", "Force trigger detected")

        # Emit "working" marker for daemon health check
        print(f"[{now_local_str('%Y-%m-%d %H:%M:%S')}] [INFO] [heartbeat] Beat sent (working)",
              file=sys.stderr)

        # Run cycle
        try:
            output = runner.run_cycle(force=force)
        except Exception as e:
            log("heartbeat", f"Cycle exception: {e}", level="error")
            output = ""

        # Morning flush: deliver anything queued during quiet hours
        if not _in_quiet_hours():
            _flush_night_queue(jd, user_id)

        if output and not looks_like_error(output):
            if _is_duplicate_send(output, jd):
                log("heartbeat", "Suppressed duplicate send (identical message "
                    f"within {DEDUP_WINDOW_SECONDS // 3600}h)", level="warn")
                (jd / ".heartbeat_last_source").unlink(missing_ok=True)
            elif _in_quiet_hours() and not _is_urgent(_peek_source(jd)):
                _queue_for_morning(output, jd)
            else:
                delivered = _route_output(output, user_id, jd)
                _note_delivery(jd, delivered, user_id)
                _write_outbox(output, jd)
                _record_engagement(jd)
                print(f"[{now_local_str('%Y-%m-%d %H:%M:%S')}] [INFO] [heartbeat] Beat sent",
                      file=sys.stderr)
        elif output:
            log("heartbeat", "Suppressed error-like output", level="warn")
            (jd / ".heartbeat_last_source").unlink(missing_ok=True)
        else:
            print(f"[{now_local_str('%Y-%m-%d %H:%M:%S')}] [INFO] [heartbeat] Beat sent (idle)",
                  file=sys.stderr)

        time.sleep(check_interval)


if __name__ == "__main__":
    jarvis_dir = os.environ.get("JARVIS_DIR", ".")
    sys.path.insert(0, jarvis_dir)

    run_loop(
        jarvis_dir=jarvis_dir,
        memory_dir=os.environ.get("MEMORY_DIR", "memory"),
        model=os.environ.get("HEARTBEAT_MODEL", "opus"),
        work_dir=os.environ.get("WORK_DIR", ""),
        check_interval=int(os.environ.get("CHECK_INTERVAL", "10")),
        user_id=os.environ.get("USER_ID", ""),
        claude_timeout=int(os.environ.get("HEARTBEAT_TIMEOUT", "600")),
    )
