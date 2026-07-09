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
from core.delivery_deadletter import record_overdue
from core.heartbeat import HeartbeatRunner
from core.jsonl import write_jsonl
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
        st.pop("first_fail", None)
    else:
        if st.get("consec_fails", 0) == 0:
            st["first_fail"] = now  # start of the current failure streak
        st["consec_fails"] = st.get("consec_fails", 0) + 1
        log("heartbeat", f"Delivery failure #{st['consec_fails']}", level="warn")
        if (st["consec_fails"] >= DELIVERY_ALERT_THRESHOLD
                and now - st.get("last_alert", 0) > DELIVERY_ALERT_COOLDOWN):
            # Self-surviving alarm (stability backlog #7): the in-band alert
            # below rides the SAME failing channel from the SAME loop — write
            # the dead-letter copy FIRST so the daemon can raise the alarm
            # even if this send (or this whole process) dies.
            from datetime import datetime
            due = datetime.fromtimestamp(
                st.get("first_fail", now)).strftime("%Y-%m-%d %H:%M")
            record_overdue(jarvis_dir, kind="delivery_failures",
                           detail=f"{st['consec_fails']} consecutive send failures",
                           due_since=due)
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
# 40, was 20: the 7/7 spend-limit night queued 33 entries and the cap
# destroyed the 13 oldest. Length-capped entries now roll over to the next
# flush instead of expiring, so a deeper queue actually drains (3 windows/day
# plus breakpoint flushes); the 48h/5-retry expiry gate still bounds growth.
NIGHT_QUEUE_MAX = 40
BATCH_FLUSH_STAMP = ".batch_last_flush"
# Floor for the user-activity breakpoint flush AND for burning a deferred
# entry's retry budget. A successful flush with length-cap deferrals leaves
# the queue file alive, so an activity breakpoint used to re-fire on EVERY
# 10s tick while the user chatted: a multi-digest burst that contradicted
# the just-sent "下个时段补发" line, and each burst bumped the crowded-out
# tail's retries — at a deep queue that expired entries in under a minute
# with zero send failures.
BREAKPOINT_FLUSH_MIN_GAP_S = 900
PROMPT_VARIANTS_FILE = ".heartbeat_prompt_variants"


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


def _clear_delivery_sidecars(jarvis_dir: Path) -> None:
    (jarvis_dir / ".heartbeat_last_source").unlink(missing_ok=True)
    (jarvis_dir / PROMPT_VARIANTS_FILE).unlink(missing_ok=True)


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


def _read_flush_stamp(jarvis_dir: Path) -> float:
    """Epoch of the last successful batch flush (0.0 when none recorded)."""
    try:
        return float((jarvis_dir / BATCH_FLUSH_STAMP).read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _should_flush(jarvis_dir: Path, minutes_of_day: int | None = None,
                  now: float | None = None) -> bool:
    """Flush the batch queue when a batch window has opened since the last
    flush, or on a user-activity breakpoint. Never during quiet hours."""
    queue_path = jarvis_dir / NIGHT_QUEUE_FILE
    if not queue_path.exists():
        return False
    if _in_quiet_hours(minutes_of_day):
        return False
    now = now if now is not None else time.time()
    last_flush = _read_flush_stamp(jarvis_dir)
    # Min-gap floor on the breakpoint path only: deferred (length-capped)
    # entries keep the queue file alive after a SUCCESSFUL flush, so an
    # unconditional True here re-flushed on every 10s tick while the user
    # was at the phone — a multi-digest burst right after promising
    # "下个时段补发". An active user with the floor unmet falls through to
    # the window arithmetic below, which stays exactly as it was.
    if (_user_recently_active(now)
            and now - last_flush > BREAKPOINT_FLUSH_MIN_GAP_S):
        return True
    if minutes_of_day is None:
        from core.timeutil import now_local
        t = now_local()
        minutes_of_day = t.hour * 60 + t.minute
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

# ── Flush accounting + bounded queue growth (stability backlog #4) ───
# The old flush was a bool that silently dropped (overflow, length cap,
# scrub) or silently retried forever: a queue entry whose flush kept failing
# lived forever and left no trail. Now every entry that LEAVES the queue gets
# one accounting row, and entries out of age/retry budget expire to the audit
# instead of living forever.
FLUSH_DELIVERED = "delivered"    # digest went out
FLUSH_RETRYABLE = "retryable"    # transient send failure — entries stay queued
FLUSH_PERMANENT = "permanent"    # can never deliver — entries moved to audit
QUIET_FLUSH_AUDIT_FILE = "data/quiet_flush_audit.jsonl"
QUIET_FLUSH_AUDIT_KEEP = 500                             # lines kept on rewrite
QUIET_FLUSH_AUDIT_REWRITE_AT = 2 * QUIET_FLUSH_AUDIT_KEEP  # rewrite past this
NIGHT_ENTRY_MAX_AGE_S = 48 * 3600  # queued longer than this → expired
NIGHT_FLUSH_MAX_RETRIES = 5        # failed flush attempts before an entry expires
# Terminal drops keep near-full text in their audit row: the 80-char preview
# made the 7/7-7/8 drops (31 of 57 queued entries) permanently unrecoverable —
# the queue file held the only full copy and it is unlinked after flush.
AUDIT_DROP_TEXT_CHARS = 2000


def _audit_flush(jarvis_dir: Path, entries: list, status: str, detail: str = ""):
    """Append one accounting row per entry to the quiet-flush audit JSONL.

    Capped: past QUIET_FLUSH_AUDIT_REWRITE_AT lines the file is rewritten to
    the last QUIET_FLUSH_AUDIT_KEEP (amortized — not on every append)."""
    if not entries:
        return
    path = jarvis_dir / QUIET_FLUSH_AUDIT_FILE
    ts = now_local_str("%Y-%m-%d %H:%M")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for e in entries:
                row = {"ts": ts, "status": status,
                       "source": e.get("source", "heartbeat"),
                       "queued_ts": e.get("ts", ""),
                       "retries": int(e.get("retries", 0) or 0),
                       "text_preview": (e.get("text") or "")[:80]}
                if status in ("expired", FLUSH_PERMANENT):
                    # This row is about to become the only surviving copy.
                    row["text"] = (e.get("text") or "")[:AUDIT_DROP_TEXT_CHARS]
                if detail:
                    row["detail"] = detail
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(path, encoding="utf-8") as f:
            n_lines = sum(1 for _ in f)
        if n_lines > QUIET_FLUSH_AUDIT_REWRITE_AT:
            _trim_file(path, QUIET_FLUSH_AUDIT_KEEP)
    except OSError as e:
        log("heartbeat", f"quiet flush audit append failed: {e}", level="warn")


def _entry_age_seconds(entry: dict, now: float) -> float:
    """Age of a queued entry. Prefers the epoch field (written since backlog
    #4); falls back to parsing the local ts string for pre-existing entries.
    Unparseable/absent timestamps count as infinitely old — a malformed entry
    must expire to the audit, not live in the queue forever."""
    epoch = entry.get("epoch")
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool):
        return now - epoch
    from datetime import datetime

    from core.timeutil import now_local
    try:
        queued = datetime.strptime(str(entry.get("ts", "")), "%Y-%m-%d %H:%M")
    except ValueError:
        return float("inf")
    # Compare in the clock that wrote the ts string (now_local, not time.time
    # arithmetic) — same TZ rationale as _is_duplicate_send.
    return (now_local().replace(tzinfo=None) - queued).total_seconds()


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
    prompt_variants = _consume_prompt_variants(jarvis_dir)
    sources = _sources_of(source)
    if sources and sources <= SILENT_SOURCES:
        # Pure silent-task output: never queue, never deliver — log only.
        log("heartbeat", f"Dropped silent-task output instead of queueing (source={source})")
        sched_emit(jarvis_dir, "task_skip", task=source, reason="silent_output")
        return
    # epoch rides along for the age-based expiry check (backlog #4) — the ts
    # string stays the display/legacy field.
    entry = {"ts": now_local_str("%Y-%m-%d %H:%M"), "epoch": int(time.time()),
             "text": readable, "source": source or "heartbeat"}
    if prompt_variants:
        entry["prompt_variants"] = prompt_variants
    with open(jarvis_dir / NIGHT_QUEUE_FILE, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    quiet = _in_quiet_hours()
    reason = "quiet hours" if quiet else "daytime batching"
    log("heartbeat", f"Queued for batch ({reason}, source={source or 'heartbeat'})")
    # Replay trail: delivery deferred to the batch queue (this is exactly the
    # hop that made the 6/12 daily-plan incident hard to reconstruct).
    sched_emit(jarvis_dir, "task_skip", task=source or "heartbeat",
               reason="queued_quiet_hours" if quiet else "queued_daytime_batch")


def _shadow_audit_claims(output: str, jarvis_dir: Path) -> None:
    """REQ-88 shadow write-claim audit, heartbeat delivery path.

    The bot.sh reply hook only sees interactive replies, but most real
    "已记录/已写入" claims ride heartbeat cards/digests (7/8 audit: 7 of 9
    claim-bearing messages flowed here unaudited). Mirror of that hook's
    contract: record-only, runs on the human-visible text (never raw card
    JSON), never sends, never raises — delivery must not depend on it.
    When extraction yields nothing human-visible the audit is SKIPPED —
    scanning the raw envelope/card JSON instead would count claims no human
    ever saw and skew the REQ-88 gate-review confirmed rate.
    """
    try:
        from tasks.write_claim_audit import audit_message
        readable = extract_readable_from_output(output)
        if not readable:
            return
        audit_message(readable, str(jarvis_dir), channel="heartbeat")
    except Exception:
        pass


def _flush_night_queue(jarvis_dir: Path, user_id: str) -> str:
    """Send queued night messages as one digest.

    Tri-state return (stability backlog #4): FLUSH_DELIVERED when the digest
    went out, FLUSH_RETRYABLE on a transient send failure (entries stay
    queued with a bumped retry count), FLUSH_PERMANENT when delivery can
    never succeed in this process (no user_id — entries move to the audit
    instead of queueing forever). "" when there was nothing to send
    (missing/scrubbed-empty queue). Every entry that leaves the queue gets
    one accounting row in data/quiet_flush_audit.jsonl; entries the digest
    budget can't fit stay queued for the next flush (7/8 audit: they used to
    expire, silently destroying 31 of 57 entries in two days).
    """
    queue_path = jarvis_dir / NIGHT_QUEUE_FILE
    if not queue_path.exists():
        return ""
    now = time.time()
    all_lines = queue_path.read_text().splitlines()
    overflow = []
    if len(all_lines) > NIGHT_QUEUE_MAX:
        # No silent caps: say what was dropped — and account for it
        log("heartbeat", f"Night queue overflow: dropping {len(all_lines) - NIGHT_QUEUE_MAX} "
            f"oldest of {len(all_lines)} entries", level="warn")
        for line in all_lines[:-NIGHT_QUEUE_MAX]:
            try:
                overflow.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
        _audit_flush(jarvis_dir, overflow, "expired", detail="queue_overflow")
        if overflow:
            # ONE aggregated dead-letter row, not one per entry — 7/7 would
            # have paged 13 lines at once. Reuses the existing kind so the
            # daemon's plain-Chinese label applies.
            record_overdue(jarvis_dir, kind="night_queue_expired",
                           detail=f"攒批队列满了，最早的 {len(overflow)} 条被挤掉"
                                  "（原文留了底，想看可以问我）",
                           due_since=overflow[0].get("ts", ""))
    entries, seen_texts, expired = [], set(), []
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
        # Bounded growth (backlog #4): an entry past its age or retry budget
        # stops living in the queue — it moves to the audit as expired.
        if (_entry_age_seconds(e, now) > NIGHT_ENTRY_MAX_AGE_S
                or int(e.get("retries", 0) or 0) >= NIGHT_FLUSH_MAX_RETRIES):
            expired.append(e)
            continue
        # Same content queued twice overnight (e.g. a task re-emitting) reads
        # as a bug to the user — keep the first occurrence only.
        if e.get("text") in seen_texts:
            continue
        seen_texts.add(e.get("text"))
        entries.append(e)
    if expired:
        log("heartbeat", f"Night queue: {len(expired)} entr(ies) expired "
            f"(>{NIGHT_ENTRY_MAX_AGE_S // 3600}h old or ≥{NIGHT_FLUSH_MAX_RETRIES} "
            f"failed flushes) — moved to audit", level="warn")
        _audit_flush(jarvis_dir, expired, "expired")
        for e in expired:
            # One dead-letter line per overdue delivery (backlog #7): the
            # daemon can tell the user about it even if this loop dies.
            record_overdue(jarvis_dir, kind="night_queue_expired",
                           detail=f"{e.get('source', 'heartbeat')}: "
                                  f"{(e.get('text') or '')[:80]}",
                           due_since=e.get("ts", ""))
    if not entries:
        queue_path.unlink(missing_ok=True)
        return ""

    dropped_for_good = len(overflow) + len(expired)
    included, pieces, len_dropped = [], [], []
    # Budget with the pre-cap count (±1 digit vs the real header below, which
    # states the DELIVERED count — it used to count entries the length cap
    # then dropped: 7/8 said "15 条" while delivering 9).
    used = len(f"📦 **攒批的 {len(entries)} 条消息**")
    # Fair split of the digest budget: one entry can use almost all of it,
    # many entries each get at least the old 600-char floor.
    per_entry = max(NIGHT_ENTRY_MAX_CHARS,
                    NIGHT_DIGEST_MAX_CHARS // len(entries) - 64)
    for e in entries:
        text = _truncate_entry(e.get("text", ""), per_entry)
        piece = f"\n— {e.get('ts', '')} · {e.get('source', '')} —\n{text}"
        # Over-budget entries roll over to the next flush (13:30/17:30 window
        # or a breakpoint) instead of expiring — they used to be destroyed
        # with only an 80-char trace (7/7: 12 of 20, incl. 行动-tagged items).
        # The first entry always ships so a lone oversized entry can't wedge
        # the FIFO forever.
        if included and used + len(piece) > NIGHT_DIGEST_MAX_CHARS:
            len_dropped.append(e)
            continue
        included.append(e)
        pieces.append(piece)
        used += len(piece)
    parts = [f"📦 **攒批的 {len(included)} 条消息**"] + pieces
    if len_dropped:
        parts.append(f"\n（还有 {len(len_dropped)} 条这次放不下，下个时段补发）")
        log("heartbeat", f"Night digest length cap: {len(len_dropped)} entries "
            "deferred to next flush", level="warn")
    if dropped_for_good:
        # Drops must never be silent to the user — only the length-cap count
        # used to be disclosed, never overflow/expiry.
        parts.append(f"\n（另有 {dropped_for_good} 条积压过久没能送出，"
                     "原文留了底，想看可以问我）")
    digest = "\n".join(parts)

    if not user_id:
        # Permanent: USER_ID is fixed for the process lifetime, so no retry
        # can ever succeed — account and clear instead of queueing forever.
        log("heartbeat", "Night queue flush impossible (no user_id) — "
            f"{len(entries)} entr(ies) moved to audit", level="warn")
        _audit_flush(jarvis_dir, entries, FLUSH_PERMANENT, detail="no user_id")
        for e in entries:
            record_overdue(jarvis_dir, kind="night_queue_undeliverable",
                           detail=f"{e.get('source', 'heartbeat')}: "
                                  f"{(e.get('text') or '')[:80]}",
                           due_since=e.get("ts", ""))
        queue_path.unlink(missing_ok=True)
        return FLUSH_PERMANENT

    if _lark_send_text(digest, user_id):
        if len_dropped:
            # Deferred entries go back in for the next flush (NOT the old
            # unconditional unlink). retries++ so an entry that never fits
            # eventually expires into the full-text audit instead of looping
            # — but only when this flush stands alone: in a burst (previous
            # successful flush <BREAKPOINT_FLUSH_MIN_GAP_S ago) a deferral
            # just means "crowded out by position", and bumping would burn a
            # deep queue's tail to expiry with zero send failures.
            if time.time() - _read_flush_stamp(jarvis_dir) \
                    > BREAKPOINT_FLUSH_MIN_GAP_S:
                for e in len_dropped:
                    e["retries"] = int(e.get("retries", 0) or 0) + 1
            write_jsonl(queue_path, len_dropped)
        else:
            queue_path.unlink(missing_ok=True)
        _stamp_flush(jarvis_dir)
        _write_outbox(digest, jarvis_dir)
        # Engagement accounting: queued sends bypassed _record_engagement, so
        # without this the morning digest is invisible to engagement-analyze.
        # Deferred entries are NOT counted sent — they will be when delivered.
        ts = now_local_str("%Y-%m-%d %H:%M")
        epoch = int(time.time())
        digest_ids = list(_LAST_SENT_IDS)
        _LAST_SENT_IDS.clear()
        with open(jarvis_dir / "engagement_log.jsonl", "a") as f:
            for source in sorted({e.get("source", "heartbeat") for e in included}):
                row = {"ts": ts, "source": source, "type": "sent",
                       "via": "night-digest", "epoch": epoch}
                for e in included:
                    if e.get("source", "heartbeat") != source:
                        continue
                    _merge_prompt_variant(row, e.get("prompt_variants", {}).get(source))
                    break
                if digest_ids:
                    row["message_ids"] = digest_ids
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        log("heartbeat", f"Flushed night queue ({len(included)} entries"
            + (f", {len(len_dropped)} deferred" if len_dropped else "") + ")")
        sched_emit(jarvis_dir, "batch_flush", count=len(included),
                   sources=sorted({e.get("source", "heartbeat") for e in included}),
                   deferred_len_cap=len(len_dropped),
                   dropped=dropped_for_good)
        _audit_flush(jarvis_dir, included, FLUSH_DELIVERED)
        _audit_flush(jarvis_dir, len_dropped, FLUSH_RETRYABLE,
                     detail="digest_length_cap")
        _shadow_audit_claims(digest, jarvis_dir)
        return FLUSH_DELIVERED
    # Retryable: entries stay queued, each carrying a bumped retry count so
    # the expiry gate above can eventually stop a forever-failing flush.
    for e in entries:
        e["retries"] = int(e.get("retries", 0) or 0) + 1
    write_jsonl(queue_path, entries)
    _audit_flush(jarvis_dir, entries, FLUSH_RETRYABLE)
    log("heartbeat", "Night queue flush failed — will retry next cycle", level="warn")
    return FLUSH_RETRYABLE


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
    prompt_variants = _consume_prompt_variants(jarvis_dir)

    sent_ids = list(_LAST_SENT_IDS)
    _LAST_SENT_IDS.clear()

    elog = jarvis_dir / "engagement_log.jsonl"
    with open(elog, "a") as f:
        for src in sources.split(","):
            src = src.strip()
            # Never log a "sent" for a SILENT source (REQ-61): in a MIXED cycle
            # (a silent task riding with a non-silent one) the silent task's
            # content is dropped at the delivery gate, yet it still got a
            # "sent" row here — making daily-plan/self-diagnostic show up as
            # guaranteed-0% sources that skew every keep/cut decision.
            if src and src not in SILENT_SOURCES:
                entry = {"ts": ts, "source": src, "type": "sent", "epoch": epoch}
                _merge_prompt_variant(entry, prompt_variants.get(src))
                if sent_ids:
                    # join key for read-receipt events (REQ-15)
                    entry["message_ids"] = sent_ids
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Back-fill the intent card ledger (REQ-34B): intentions_post recorded
    # which intent ids this cycle's card covers; now that the real Lark
    # message_ids are known, stamp them in so a quote-reply to the card can
    # be matched back to its intent deterministically.
    if sent_ids and "intention-check" in sources:
        try:
            _ledger_backfill(jarvis_dir, sent_ids)
        except Exception as e:
            log("heartbeat", f"intent ledger backfill failed: {e}", level="warn")


def _consume_prompt_variants(jarvis_dir: Path) -> dict:
    path = jarvis_dir / PROMPT_VARIANTS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        data = {}
    path.unlink(missing_ok=True)
    return data if isinstance(data, dict) else {}


def _merge_prompt_variant(row: dict, variant: dict | None) -> None:
    if not isinstance(variant, dict):
        return
    exp = variant.get("prompt_experiment")
    vid = variant.get("prompt_variant")
    if exp:
        row["prompt_experiment"] = str(exp)[:80]
    if vid:
        row["prompt_variant"] = str(vid)[:80]


def _ledger_backfill(jarvis_dir: Path, message_ids: list):
    """Stamp real message_ids onto the newest ledger row lacking them.

    Red-team fix: an UNSTAMPED row older than the newest means a prior card's
    backfill was missed (two cards sent before this runs) — its message_ids
    are unrecoverable, so it can never match a reply. Leaving it unstamped
    strands it forever AND lets the file fill with dead rows. So: stamp the
    newest unstamped row with THIS cycle's ids, and DROP any older unstamped
    rows (they're useless for reply matching) — the ledger only ever holds
    rows that are either stamped or the single most-recent pending one.
    """
    ledger = jarvis_dir / "data" / ".intent_card_ledger.jsonl"
    if not ledger.exists():
        return
    parsed = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    stamped_newest = False
    kept = []
    for row in reversed(parsed):  # newest first
        if row.get("message_ids"):
            kept.append(row)
        elif not stamped_newest:
            row["message_ids"] = list(message_ids)
            kept.append(row)
            stamped_newest = True
        # else: an older unstamped row — drop it (unrecoverable, would strand)
    kept.reverse()
    kept = kept[-200:]  # bounded — only recent cards matter for reply matching
    tmp = ledger.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in kept)
                   + ("\n" if kept else ""), encoding="utf-8")
    os.replace(tmp, ledger)


def _trim_file(path: Path, max_lines: int):
    """Trim a JSONL file to its last N lines, append-safely (REQ-49).

    The old read→tmp→os.replace silently destroyed lines appended between the
    read and the replace — engagement_log has concurrent appenders (bot.sh jq
    read-receipts), and lost rows corrupt the stats driving interval
    auto-tuning. In-place truncate+rewrite under flock keeps the same inode
    (O_APPEND writers never get diverted to a dead file) and shrinks the race
    window from the whole trim to nothing for flock-takers and ~ms for the
    unlocked jq appenders.
    """
    import fcntl
    if not path.exists():
        return
    try:
        with open(path, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            lines = f.read().splitlines()
            if len(lines) > max_lines:
                f.seek(0)
                f.truncate()
                f.write("\n".join(lines[-max_lines:]) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        log("heartbeat", f"trim failed for {path.name}: {e}", level="warn")


def _hourly_housekeeping(jd: Path):
    """Hourly GC pass (REQ-49) — runs on the loop's tick, never raises.

    - re-trim outbox/engagement (startup-only trims let week-long uptimes
      grow them unbounded)
    - jobs GC: cleanup_old_jobs was dead code; 18 of 20 job dirs were empty
      husks that every future audit re-investigated
    - views GC: expired RichView JSONs previously lingered until someone
      happened to GET them
    - /tmp logs: launchd appends to jarvis-dashboard.log forever; restart.log
      only rotated inside restart.sh
    """
    try:
        _trim_file(jd / "heartbeat_outbox.jsonl", 20)
        _trim_file(jd / "engagement_log.jsonl", 2000)
    except Exception as e:
        log("heartbeat", f"housekeeping trim error: {e}", level="warn")
    try:
        from core.jobs import JobManager
        removed = JobManager(jd / "jobs").cleanup_old_jobs(max_age_days=7)
        if removed:
            log("heartbeat", f"jobs GC: removed {removed} finished job(s) >7d")
    except Exception as e:
        log("heartbeat", f"jobs GC error: {e}", level="warn")
    try:
        import time as _t
        views = jd / "views"
        if views.is_dir():
            n = 0
            for vf in views.glob("*.json"):
                try:
                    expires = json.loads(vf.read_text()).get("expires_at", 0)
                    if expires and _t.time() > expires:
                        vf.unlink(missing_ok=True)
                        n += 1
                except (json.JSONDecodeError, OSError):
                    continue
            if n:
                log("heartbeat", f"views GC: removed {n} expired view(s)")
    except Exception as e:
        log("heartbeat", f"views GC error: {e}", level="warn")
    for tmp_log in (Path("/tmp/jarvis-dashboard.log"),
                    Path("/tmp/jarvis_restart.log")):
        try:
            if tmp_log.exists() and tmp_log.stat().st_size > 500_000:
                lines = tmp_log.read_text(errors="replace").splitlines()
                # copytruncate: launchd/daemon hold O_APPEND fds — a rename
                # would divert their writes to a dead inode until restart.
                with open(tmp_log, "r+") as f:
                    f.seek(0)
                    f.truncate()
                    f.write("\n".join(lines[-500:]) + "\n")
        except OSError:
            pass


SLEEP_GAP_THRESHOLD_S = 120


def _sleep_gap_seconds(slept_for_s: float, expected_s: float,
                       threshold_s: float = SLEEP_GAP_THRESHOLD_S) -> float:
    """Return host sleep/pause gap beyond the expected sleep interval.

    This is measured only around the loop's own short sleep, not around Claude
    calls, so long model runs are not mislabeled as host sleep.
    """
    gap = slept_for_s - expected_s
    return gap if gap >= threshold_s else 0.0


# "Beat sent" lines are load-bearing, not narration: daemon.py's
# _find_last_heartbeat greps the newest one from jarvis.log and RESTARTS the
# heartbeat when it is older than HEARTBEAT_STALE_THRESHOLD=1800s, and
# scripts/doctor.sh + tests/test_integration.py grep the same string. But the
# per-tick version (working+idle every 10s) was 65% of jarvis.log, rotating
# real history away in ~6h (2026-07-07). The throttle interval must stay WELL
# under 1800s: the pre-cycle beat may be suppressed for up to the full
# interval before a long multi-task cycle starts, and interval + cycle
# duration must not cross the daemon's staleness threshold.
#
# DO NOT raise this back above 120 (red-team 7/8): worst-case beat gap =
# full suppression (120s) + one heavy solo task (HEAVY_DEFAULT_TIMEOUT=900s,
# core/heartbeat.py) + one shared batch call (HEARTBEAT_TIMEOUT default
# 600s) = 1620s < 1800s stale threshold. At the previous 600s the same cycle
# reached ~2200s and the daemon restarted the stack mid-heavy-task. 120s is
# still a 12x spam reduction vs per-tick. Machine-checked by
# tests/test_heartbeat_loop.py::test_beat_interval_plus_max_cycle_stays_under_stale_threshold.
BEAT_LOG_INTERVAL_S = 120

# Liveness stamp (7/8 audit F15): the daemon's primary liveness check now
# stats this file's mtime — log-tail parsing of 'Beat sent' regressed three
# times (10KB tail, tight JSON regex, beats[-1] masking) and stays only as
# the daemon's fallback for the mixed-version deploy window. Set by run_loop
# (jd/data/.heartbeat_beat); module-level so _beat can touch it on EVERY
# invocation, including throttled ones — one utime syscall proves liveness
# even when the log line is suppressed.
_BEAT_STAMP_PATH: Path | None = None


def _init_beat_stamp(jd: Path) -> None:
    global _BEAT_STAMP_PATH
    _BEAT_STAMP_PATH = jd / "data" / ".heartbeat_beat"
    try:
        _BEAT_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _beat(label: str, *, force: bool = False) -> bool:
    """Emit one 'Beat sent (label)' liveness line, throttled to
    BEAT_LOG_INTERVAL_S; touch _BEAT_STAMP_PATH unthrottled first. Returns
    True if the line was emitted.

    State is a function attribute so a fresh process always emits its first
    beat — doctor.sh's "wait ~30s after start" hint and test_integration's
    beat count rely on that. Line format must stay matchable by daemon.py's
    regex: [YYYY-MM-DD HH:MM:SS] … heartbeat … Beat sent (fallback liveness
    path — see _BEAT_STAMP_PATH).
    """
    if _BEAT_STAMP_PATH is not None:
        try:
            _BEAT_STAMP_PATH.touch()
        except OSError:
            pass  # read-only / full disk must not kill the loop
    now = time.time()
    if not force and now - getattr(_beat, "_last_emit", 0.0) < BEAT_LOG_INTERVAL_S:
        return False
    _beat._last_emit = now
    print(f"[{now_local_str('%Y-%m-%d %H:%M:%S')}] [INFO] [heartbeat] Beat sent ({label})",
          file=sys.stderr)
    return True


def run_loop(jarvis_dir: str, memory_dir: str, model: str = "opus",
             work_dir: str = "", check_interval: int = 10, user_id: str = "",
             claude_timeout: int = 600):
    """Main heartbeat loop. Runs forever until killed."""
    jd = Path(jarvis_dir)
    heartbeat_trigger = Path("/tmp/jarvis-heartbeat-trigger")
    _init_beat_stamp(jd)

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
        # Optional explicit override; empty ⇒ resolve_claude_bin uses
        # which + ~/.local/bin fallback (severs the launchd-PATH dependency).
        claude_bin=os.environ.get("CLAUDE_BIN", ""),
    )

    log("heartbeat", f"Starting ({check_interval}s cycle)")

    ticks = 0
    was_active = False  # previous tick produced output (working↔idle edge)
    while True:
        ticks += 1
        # Background-job sweeper (REQ-16 MVP-3, ~every 60s): a job whose
        # handler died (crash/restart) would otherwise stay "running" forever
        # and the user waits on a result that will never come.
        if ticks % max(1, 3600 // max(1, check_interval)) == 0:
            _hourly_housekeeping(jd)
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
        # Check restart trigger. Single consumer, fast path (REQ-42): spawn
        # restart.sh detached and exit the loop NORMALLY — the old behavior
        # (SIGTERM the whole process group, then wait for daemon.py to notice
        # over 2x30s checks + 300s cooldown) gave 1-15 minutes of darkness,
        # racing a second consumer in bot.sh that only ran when a Lark
        # message happened to arrive. restart.sh handles the actual kill.
        restart_trigger = jd / ".restart_trigger"
        if restart_trigger.exists():
            restart_trigger.unlink(missing_ok=True)
            log("heartbeat", "Restart trigger detected — handing off to restart.sh")
            try:
                subprocess.Popen(["bash", str(jd / "restart.sh"), "--yes"],
                                 start_new_session=True,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as e:
                log("heartbeat", f"restart.sh spawn failed: {e} — falling back "
                    "to group SIGTERM", level="error")
                os.kill(0, signal.SIGTERM)
            break

        # Check force trigger. The file is an APPEND log (red-team fix): each
        # force is one line (admin 'Run Now', chat [ACTION:heartbeat]); we
        # drain EVERY line so two near-simultaneous forces are never dropped
        # last-writer-wins, and an O_APPEND line is never read torn. A bare
        # task name scopes a cycle to run_cycle(only_task=); empty/'all' is a
        # full-roster force behind a 10-min cooldown (32 storms/day were
        # re-running weekly tasks within hours and starving the batch queue).
        force_tasks: list[str] = []   # distinct named tasks to force
        want_full = False
        if heartbeat_trigger.exists():
            try:
                _lines = heartbeat_trigger.read_text().splitlines()
            except OSError:
                _lines = []
            heartbeat_trigger.unlink(missing_ok=True)
            for _ln in _lines:
                t = _ln.strip()[:64]
                if not t or t == "all":
                    want_full = True
                elif t not in force_tasks:
                    force_tasks.append(t)
            if force_tasks:
                log("heartbeat", f"Force trigger(s): {force_tasks}")

        # "working" marker for the daemon health check. BEFORE the cycle so a
        # long multi-task cycle doesn't look dead, throttled (see _beat) so
        # steady 10s ticks don't rotate jarvis.log history away.
        _beat("working")

        # Run cycle(s). Forced runs REPLACE the normal cadence cycle this tick
        # (matches prior semantics); when nothing is forced, the normal
        # due-check runs.
        try:
            if force_tasks or want_full:
                _outputs = []
                for _t in force_tasks:
                    o = runner.run_cycle(force=True, only_task=_t)
                    if o:
                        _outputs.append(o)
                if want_full:
                    last_full = getattr(run_loop, "_last_full_force", 0.0)
                    if time.time() - last_full >= 600:
                        run_loop._last_full_force = time.time()
                        o = runner.run_cycle(force=True)
                        if o:
                            _outputs.append(o)
                    else:
                        log("heartbeat", "Full-roster force suppressed "
                            "(within 10min cooldown)", level="warn")
                output = "\n\n---\n\n".join(_outputs)
            else:
                output = runner.run_cycle(force=False)
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
                _clear_delivery_sidecars(jd)
            elif _is_duplicate_send(output, jd):
                log("heartbeat", "Suppressed duplicate send (identical message "
                    f"within {DEDUP_WINDOW_SECONDS // 3600}h)", level="warn")
                sched_emit(jd, "task_skip",
                           task=",".join(sorted(cycle_sources)) or "heartbeat",
                           reason="duplicate_send")
                _clear_delivery_sidecars(jd)
            elif _should_queue(jd):
                _queue_for_morning(output, jd)
            else:
                delivered = _route_output(output, user_id, jd)
                _note_delivery(jd, delivered, user_id)
                if delivered:
                    _write_outbox(output, jd)
                    _record_engagement(jd)
                    _shadow_audit_claims(output, jd)
                    print(f"[{now_local_str('%Y-%m-%d %H:%M:%S')}] [INFO] [heartbeat] Beat sent",
                          file=sys.stderr)
                else:
                    # Do NOT write outbox/engagement on failure: an outbox
                    # entry would make the dedup window suppress the retry
                    # (REQ-04 cancelling REQ-11), and a "sent" record would
                    # poison engagement stats with messages never delivered.
                    _clear_delivery_sidecars(jd)
                    _LAST_SENT_IDS.clear()  # drop ids from partial successes
                    log("heartbeat", "Delivery failed — output not recorded as sent",
                        level="warn")
        elif output:
            log("heartbeat", "Suppressed error-like output", level="warn")
            _clear_delivery_sidecars(jd)
        else:
            # Transition-only (working→idle): steady idle is covered by the
            # throttled (working) line above; per-tick idle beats were the
            # bulk of the 65% log spam.
            if was_active:
                _beat("idle", force=True)
        was_active = bool(output)

        sleep_started = time.time()
        time.sleep(check_interval)
        gap = _sleep_gap_seconds(time.time() - sleep_started, check_interval)
        if gap:
            sched_emit(jd, "sleep_gap", source="heartbeat_loop",
                       duration_s=round(gap, 1),
                       slept_for_s=round(gap + check_interval, 1),
                       expected_s=check_interval)


if __name__ == "__main__":
    jarvis_dir = os.environ.get("JARVIS_DIR", ".")
    sys.path.insert(0, jarvis_dir)

    # Process-level singleton (red-team fix): the REQ-42 restart hand-off can
    # briefly leave two heartbeat_loops alive (the bot.sh watchdog relaunches
    # one while restart.sh tears the tree down). A duplicate that grabbed the
    # cycle lock and ran intention-check would corrupt the inflight manifest /
    # double-process intents. An exclusive flock held for process lifetime
    # makes the second instance exit immediately — harmless no matter who
    # spawned it. The fd is intentionally leaked (kept open until exit).
    import fcntl as _fcntl
    _lock_path = os.path.join(jarvis_dir, "data", ".heartbeat_loop.lock")
    try:
        os.makedirs(os.path.dirname(_lock_path), exist_ok=True)
        _lock_fd = open(_lock_path, "w")
        _fcntl.flock(_lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
    except OSError:
        log("heartbeat", "another heartbeat_loop holds the singleton lock — exiting",
            level="warn")
        sys.exit(0)

    run_loop(
        jarvis_dir=jarvis_dir,
        memory_dir=os.environ.get("MEMORY_DIR", "memory"),
        model=os.environ.get("HEARTBEAT_MODEL", "opus"),
        work_dir=os.environ.get("WORK_DIR", ""),
        check_interval=int(os.environ.get("CHECK_INTERVAL", "10")),
        user_id=os.environ.get("USER_ID", ""),
        claude_timeout=int(os.environ.get("HEARTBEAT_TIMEOUT", "600")),
    )
