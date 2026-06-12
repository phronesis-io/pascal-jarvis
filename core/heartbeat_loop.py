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
from core.sched_events import emit as sched_emit
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
                mid = _extract_message_id(r.stdout)
                if mid:
                    _LAST_SENT_IDS.append(mid)
                return True
        except subprocess.TimeoutExpired:
            # A local timeout does NOT mean the server didn't get it — on a
            # slow link the message is usually already delivered. Returning
            # False here made _route_output re-send the content as TEXT (a
            # duplicate), skip the outbox (so dedup couldn't stop the task's
            # next re-emission → third copy), and count a delivery failure
            # toward the user alert. Assume delivered: worst case one message
            # is silently lost on a true network drop, vs guaranteed
            # duplicates the other way.
            log("heartbeat", "lark_send_card timed out — assuming delivered, no retry",
                level="warn")
            return True
        except Exception as e:
            log("heartbeat", f"lark_send_card attempt {attempt} failed: {e}", level="warn")
    return False


# Retry sleeps between send attempts (REQ-11). Transient lark-cli/network
# hiccups were surfacing as silent message loss the user discovered by hand.
SEND_RETRY_DELAYS = (2, 5)

# message_ids of sends in the current cycle (REQ-15): _record_engagement
# drains this into the "sent" entries so read-receipt events
# (message_id_list) can be joined precisely instead of by time proximity.
_LAST_SENT_IDS: list = []


def _extract_message_id(stdout: str) -> str:
    try:
        obj = json.loads(stdout or "")
    except (json.JSONDecodeError, ValueError):
        return ""
    if isinstance(obj, dict):
        mid = obj.get("message_id") or (obj.get("data") or {}).get("message_id")
        return mid if isinstance(mid, str) else ""
    return ""


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
                mid = _extract_message_id(r.stdout)
                if mid:
                    _LAST_SENT_IDS.append(mid)
                return True
        except subprocess.TimeoutExpired:
            # Local timeout ≠ undelivered; assume delivered (see card variant
            # for the duplicate-vs-loss tradeoff analysis).
            log("heartbeat", "lark_send_text timed out — assuming delivered, no retry",
                level="warn")
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


# ── Quiet hours + daytime batching queue (REQ-13) ───────────────────
# Empirics (docs/research/engagement_hourly_baseline.md): 0-9h replies ≤6-9%,
# 9h still ~6%, golden windows 10-11/14/18/21h at 70%+. Literature (Fitz 2019
# RCT): ~3 notification batches/day beats both hourly and full suppression;
# defer-to-breakpoint beats wall-clock delivery (Iqbal & Bailey CHI'08,
# Fischer MobileHCI'11). Mechanism:
# - quiet hours (23:30-10:00): every non-urgent message queues
# - daytime: general-interest sources (feed/recommendations) queue too,
#   flushed as one digest at the batch windows; task-relevant sources
#   (checkin, intents, calendar, team monitor) still send immediately
# - breakpoint release: if the user messaged within the last 5 minutes
#   (they're at the phone — a natural breakpoint), flush early

QUIET_START_MIN = 23 * 60 + 30   # 23:30
QUIET_END_MIN = 10 * 60          # 10:00 (was 09:30; 9h replies were still ~6%)
URGENT_SOURCES = {"intention-check", "calendar-sync", "checkin"}
# General-interest content per Iqbal & Bailey: tolerates coarse batching with
# the least frustration cost. These are also the highest-volume noise sources.
GENERAL_INTEREST_SOURCES = {"eigenflux-feed-triage", "content-recommend",
                            "personal-site"}
# Permanently silent housekeeping tasks: never delivered, never queued — logs
# only. The exact task→message filter lives in HeartbeatRunner._collect_output
# (core/heartbeat.py, the single source of truth for the name list); this
# delivery-layer backstop also scrubs legacy night_queue entries and any
# pure-silent cycle output that slips through.
SILENT_SOURCES = HeartbeatRunner.SILENT_TASKS
BATCH_WINDOWS_MIN = (10 * 60, 13 * 60 + 30, 17 * 60 + 30)  # 10:00/13:30/17:30
BREAKPOINT_RECENCY_SECONDS = 300
LAST_MSG_MARKER = "/tmp/jarvis-last-msg"  # touched by bot.sh on every inbound msg
NIGHT_QUEUE_FILE = "night_queue.jsonl"
NIGHT_QUEUE_MAX = 20
BATCH_FLUSH_STAMP = ".batch_last_flush"


def _in_quiet_hours(minutes_of_day: int | None = None) -> bool:
    if minutes_of_day is None:
        # now_local(), not time.localtime(): launchd/cron children may carry
        # TZ=UTC, which would shift the quiet window by hours. timeutil reads
        # the system tz — same source as the ts strings written everywhere.
        from core.timeutil import now_local
        t = now_local()
        minutes_of_day = t.hour * 60 + t.minute
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


def _sources_of(source_str: str) -> set:
    return {s.strip() for s in source_str.split(",") if s.strip()}


def _should_queue(jarvis_dir: Path, minutes_of_day: int | None = None) -> bool:
    """Decide whether the current output goes to the batch queue.

    Quiet hours: everything non-urgent queues. Daytime: only when ALL sources
    are general-interest (mixed task-relevant content sends immediately).

    .urgent_send flag (written by a task post-script, e.g. an urgent:true
    EigenFlux item that already cleared its own night gate) bypasses the
    queue entirely for this cycle — the task-name sidecar can't carry
    per-item urgency, so this flag does.
    """
    urgent_flag = jarvis_dir / ".urgent_send"
    if urgent_flag.exists():
        urgent_flag.unlink(missing_ok=True)
        return False
    sources = _sources_of(_peek_source(jarvis_dir))
    if _in_quiet_hours(minutes_of_day):
        return not bool(sources & URGENT_SOURCES)
    return bool(sources) and sources <= GENERAL_INTEREST_SOURCES


def _user_recently_active(now: float | None = None) -> bool:
    """True if the user sent a message within the breakpoint window —
    they're at the phone, so a delivery now interrupts nothing."""
    now = now if now is not None else time.time()
    try:
        return now - os.path.getmtime(LAST_MSG_MARKER) < BREAKPOINT_RECENCY_SECONDS
    except OSError:
        return False


def _should_flush(jarvis_dir: Path, minutes_of_day: int | None = None,
                  now: float | None = None) -> bool:
    """Flush the batch queue when a batch window has opened since the last
    flush, or on a user-activity breakpoint. Never during quiet hours."""
    queue_path = jarvis_dir / NIGHT_QUEUE_FILE
    if not queue_path.exists():
        return False
    if _in_quiet_hours(minutes_of_day):
        return False
    if _user_recently_active(now):
        return True
    if minutes_of_day is None:
        from core.timeutil import now_local
        t = now_local()
        minutes_of_day = t.hour * 60 + t.minute
    now = now if now is not None else time.time()
    try:
        last_flush = float((jarvis_dir / BATCH_FLUSH_STAMP).read_text().strip())
    except (OSError, ValueError):
        last_flush = 0.0
    # Minutes since midnight of the last flush, same-day comparison: if the
    # last flush was >24h ago any window counts.
    passed_windows = [w for w in BATCH_WINDOWS_MIN if minutes_of_day >= w]
    if not passed_windows:
        return False
    latest_window = max(passed_windows)
    seconds_since_window = (minutes_of_day - latest_window) * 60
    return now - last_flush > seconds_since_window + 60


def _stamp_flush(jarvis_dir: Path, now: float | None = None):
    now = now if now is not None else time.time()
    (jarvis_dir / BATCH_FLUSH_STAMP).write_text(str(now))


NIGHT_ENTRY_MAX_CHARS = 600     # per-entry floor when many entries share the digest
NIGHT_DIGEST_MAX_CHARS = 3800   # total digest budget (Lark text limit headroom)


def _truncate_entry(text: str, limit: int) -> str:
    """Cap an entry at `limit`, preferring a newline boundary so we never
    leave a dangling half markdown link mid-sentence."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit * 0.6:
        cut = cut[:nl]
    else:
        lb = cut.rfind("[")
        if lb != -1 and ")" not in cut[lb:]:
            cut = cut[:lb]
    return cut.rstrip() + "…(截断)"


def _queue_for_morning(output: str, jarvis_dir: Path):
    readable = extract_readable_from_output(output) or output
    # Store near-full text; the fair per-entry cap is applied at flush time
    # when the batch size is known (a single queued message used to be cut
    # to 600 chars even though the whole digest budget was free).
    readable = _truncate_entry(readable, NIGHT_DIGEST_MAX_CHARS)
    source = _peek_source(jarvis_dir)
    (jarvis_dir / ".heartbeat_last_source").unlink(missing_ok=True)
    sources = _sources_of(source)
    if sources and sources <= SILENT_SOURCES:
        # Pure silent-task output: never queue, never deliver — log only.
        log("heartbeat", f"Dropped silent-task output instead of queueing (source={source})")
        sched_emit(jarvis_dir, "task_skip", task=source, reason="silent_output")
        return
    entry = {"ts": now_local_str("%Y-%m-%d %H:%M"),
             "text": readable, "source": source or "heartbeat"}
    with open(jarvis_dir / NIGHT_QUEUE_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    quiet = _in_quiet_hours()
    reason = "quiet hours" if quiet else "daytime batching"
    log("heartbeat", f"Queued for batch ({reason}, source={source or 'heartbeat'})")
    # Replay trail: delivery deferred to the batch queue (this is exactly the
    # hop that made the 6/12 daily-plan incident hard to reconstruct).
    sched_emit(jarvis_dir, "task_skip", task=source or "heartbeat",
               reason="queued_quiet_hours" if quiet else "queued_daytime_batch")


def _flush_night_queue(jarvis_dir: Path, user_id: str) -> bool:
    """Send queued night messages as one digest. Returns True if flushed."""
    queue_path = jarvis_dir / NIGHT_QUEUE_FILE
    if not queue_path.exists():
        return False
    all_lines = queue_path.read_text().splitlines()
    if len(all_lines) > NIGHT_QUEUE_MAX:
        # No silent caps: say what was dropped
        log("heartbeat", f"Night queue overflow: dropping {len(all_lines) - NIGHT_QUEUE_MAX} "
            f"oldest of {len(all_lines)} entries", level="warn")
    entries, seen_texts = [], set()
    for line in all_lines[-NIGHT_QUEUE_MAX:]:
        try:
            e = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        # Silent housekeeping output must never reach the user, even via the
        # digest — scrubs entries queued before the SILENT_TASKS gate existed.
        entry_sources = _sources_of(e.get("source", ""))
        if entry_sources and entry_sources <= SILENT_SOURCES:
            log("heartbeat", f"Scrubbed silent-task entry from night queue "
                f"(source={e.get('source')})")
            continue
        # Same content queued twice overnight (e.g. a task re-emitting) reads
        # as a bug to the user — keep the first occurrence only.
        if e.get("text") in seen_texts:
            continue
        seen_texts.add(e.get("text"))
        entries.append(e)
    if not entries:
        queue_path.unlink(missing_ok=True)
        return False

    parts = [f"📦 **攒批的 {len(entries)} 条消息**"]
    used = len(parts[0])
    dropped = 0
    # Fair split of the digest budget: one entry can use almost all of it,
    # many entries each get at least the old 600-char floor.
    per_entry = max(NIGHT_ENTRY_MAX_CHARS,
                    NIGHT_DIGEST_MAX_CHARS // len(entries) - 64)
    for e in entries:
        text = _truncate_entry(e.get("text", ""), per_entry)
        piece = f"\n— {e.get('ts', '')} · {e.get('source', '')} —\n{text}"
        if used + len(piece) > NIGHT_DIGEST_MAX_CHARS:
            dropped += 1
            continue
        parts.append(piece)
        used += len(piece)
    if dropped:
        parts.append(f"\n（另有 {dropped} 条因长度省略）")
        log("heartbeat", f"Night digest length cap: dropped {dropped} entries", level="warn")
    digest = "\n".join(parts)

    if _lark_send_text(digest, user_id):
        queue_path.unlink(missing_ok=True)
        _stamp_flush(jarvis_dir)
        _write_outbox(digest, jarvis_dir)
        # Engagement accounting: queued sends bypassed _record_engagement, so
        # without this the morning digest is invisible to engagement-analyze.
        ts = now_local_str("%Y-%m-%d %H:%M")
        epoch = int(time.time())
        digest_ids = list(_LAST_SENT_IDS)
        _LAST_SENT_IDS.clear()
        with open(jarvis_dir / "engagement_log.jsonl", "a") as f:
            for source in sorted({e.get("source", "heartbeat") for e in entries}):
                row = {"ts": ts, "source": source, "type": "sent",
                       "via": "night-digest", "epoch": epoch}
                if digest_ids:
                    row["message_ids"] = digest_ids
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        log("heartbeat", f"Flushed night queue ({len(entries)} entries)")
        sched_emit(jarvis_dir, "batch_flush", count=len(entries),
                   sources=sorted({e.get("source", "heartbeat") for e in entries}),
                   dropped_len_cap=dropped)
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
    from datetime import datetime
    from core.timeutil import now_local
    readable = extract_readable_from_output(output) or output
    # Compare in the same clock that wrote the ts strings (now_local_str),
    # NOT time.mktime — a TZ env mismatch would shift the window by hours.
    now_dt = now_local().replace(tzinfo=None)
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
            sent_dt = datetime.strptime(entry.get("ts", ""), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if (now_dt - sent_dt).total_seconds() < DEDUP_WINDOW_SECONDS:
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

    sent_ids = list(_LAST_SENT_IDS)
    _LAST_SENT_IDS.clear()

    elog = jarvis_dir / "engagement_log.jsonl"
    with open(elog, "a") as f:
        for src in sources.split(","):
            src = src.strip()
            if src:
                entry = {"ts": ts, "source": src, "type": "sent", "epoch": epoch}
                if sent_ids:
                    # join key for read-receipt events (REQ-15)
                    entry["message_ids"] = sent_ids
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
    # 2000, not 500: read/reaction receipt events (REQ-15) share this file and
    # would otherwise crowd the sent/response history out of the window.
    _trim_file(jd / "engagement_log.jsonl", 2000)

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

    ticks = 0
    while True:
        ticks += 1
        # Background-job sweeper (REQ-16 MVP-3, ~every 60s): a job whose
        # handler died (crash/restart) would otherwise stay "running" forever
        # and the user waits on a result that will never come.
        if ticks % max(1, 60 // max(1, check_interval)) == 0:
            try:
                from core.jobs import JobManager
                lost = JobManager(jd / "jobs").sweep_lost()
                for job_id in lost:
                    log("heartbeat", f"Swept lost background job {job_id}", level="warn")
                    _lark_send_text(
                        f"⚠️ 后台任务 `{job_id}` 异常终止（进程已不在，可能因重启/崩溃）。"
                        f"需要的话告诉我任务内容，我重新跑一个。", user_id)
                if lost:
                    # Drop the alert's own message_id: nothing drains the
                    # tracker here, and a stale id would mis-attribute to the
                    # NEXT cycle's sent entries (REQ-15 join precision).
                    _LAST_SENT_IDS.clear()
            except Exception as e:
                log("heartbeat", f"Job sweep error: {e}", level="warn")
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

        # Batch flush: a window opened, or the user is at the phone
        if _should_flush(jd):
            _flush_night_queue(jd, user_id)

        if output and not looks_like_error(output, proactive=True):
            cycle_sources = _sources_of(_peek_source(jd))
            if cycle_sources and cycle_sources <= SILENT_SOURCES:
                # Backstop: pure silent-task output must never be delivered or
                # queued (primary filter is HeartbeatRunner._collect_output).
                log("heartbeat", "Suppressed silent-task output "
                    f"({','.join(sorted(cycle_sources))}) — log-only, never delivered")
                sched_emit(jd, "task_skip", task=",".join(sorted(cycle_sources)),
                           reason="silent_output")
                (jd / ".heartbeat_last_source").unlink(missing_ok=True)
            elif _is_duplicate_send(output, jd):
                log("heartbeat", "Suppressed duplicate send (identical message "
                    f"within {DEDUP_WINDOW_SECONDS // 3600}h)", level="warn")
                sched_emit(jd, "task_skip",
                           task=",".join(sorted(cycle_sources)) or "heartbeat",
                           reason="duplicate_send")
                (jd / ".heartbeat_last_source").unlink(missing_ok=True)
            elif _should_queue(jd):
                _queue_for_morning(output, jd)
            else:
                delivered = _route_output(output, user_id, jd)
                _note_delivery(jd, delivered, user_id)
                if delivered:
                    _write_outbox(output, jd)
                    _record_engagement(jd)
                    print(f"[{now_local_str('%Y-%m-%d %H:%M:%S')}] [INFO] [heartbeat] Beat sent",
                          file=sys.stderr)
                else:
                    # Do NOT write outbox/engagement on failure: an outbox
                    # entry would make the dedup window suppress the retry
                    # (REQ-04 cancelling REQ-11), and a "sent" record would
                    # poison engagement stats with messages never delivered.
                    (jd / ".heartbeat_last_source").unlink(missing_ok=True)
                    _LAST_SENT_IDS.clear()  # drop ids from partial successes
                    log("heartbeat", "Delivery failed — output not recorded as sent",
                        level="warn")
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
