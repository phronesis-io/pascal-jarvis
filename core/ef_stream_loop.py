#!/usr/bin/env python3
"""EigenFlux real-time stream loop — receives PMs and delivers to Lark.

Replaces the bash eigenflux_stream_loop() in bot.sh. Manages:
  - eigenflux stream subprocess (single instance, gateway-aware)
  - Message formatting and Lark delivery
  - PM dedup (item_id) so a reconnect can't re-deliver / re-analyze a message
  - Outbox writing for main session visibility
  - Background Claude analysis for follow-up context (with conversation history)
  - Graceful shutdown on SIGTERM/SIGINT (reap children, stop without restart)

bot.sh launches this as: python3 -m core.ef_stream_loop &

IMPORTANT: `eigenflux stream` is managed by openclaw-gateway. The gateway
reparents the stream subprocess and auto-restarts it on crash. We must NOT
pkill or create competing stream processes — that causes "Connection replaced"
loops. Instead, we spawn ONE stream and read its stdout until it dies, then
respawn with backoff.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.card import linkify_bare_urls
from core.aux_model import run_auxiliary_model
from core.delivery_deadletter import record_overdue
from core.ef_stream import (
    extract_detail,
    extract_item_ids,
    extract_metadata,
    extract_relation_ids,
    format_message,
    format_relation_event,
    is_duplicate_event,
    load_seen,
    parse_cursor,
    relation_event_kind,
    remember_seen,
    save_seen,
)
from core.log import log
from core.timeutil import now_local_str
from core import memorial

# ── Stall watchdog (audit 2026-07-10) ───────────────────────────────
# A half-open TCP connection can leave `eigenflux stream` alive but silent
# forever: the blocking `for line in proc.stdout:` never returns, the
# reconnect backoff after it is unreachable, and every supervisor
# (components.yaml pgrep, kill -0) still sees a live process. After a long
# silence we kill the child so the existing respawn+backoff path takes over.
# This is a backstop, not the primary path — the CLI reconnects on its own
# and TCP keepalive reaps most half-open connections in minutes. Quiet
# stretches with zero PMs are real, so a false-positive kill must be (and is)
# harmless: the respawn resumes from the persisted cursor and the seen-set
# dedups any replay.
STALL_KILL_AFTER_S = 30 * 60
STALL_POLL_S = 60

# REQ-95 (2026-07-14): a connection that lived this long before dropping was
# a WORKING connection that went quiet (stall-kill on an idle day, server
# rolling restart) — not a failing one. Before this, only an incoming message
# reset the backoff, so on a zero-PM day every 30-min stall-kill incremented
# `failures` forever (observed: failure #27, permanent 300s backoff = ~2h/day
# of blind windows) and the counter read like an outage.
HEALTHY_CONN_S = 10 * 60


def _is_stalled(proc, idle_s: float, threshold: float = STALL_KILL_AFTER_S) -> bool:
    """True when the stream subprocess is alive but has been silent too long."""
    return proc is not None and proc.poll() is None and idle_s > threshold


def _healthy_churn(lifetime_s: float, replaced: bool,
                   threshold: float = HEALTHY_CONN_S) -> bool:
    """True when a dropped connection should reset the reconnect backoff.

    Lifetime-based, NOT output-based: a server that emits one error line and
    closes would look "productive" and reconnect-storm at 1s. The exception:
    'Connection replaced' (another session took the stream) must keep the
    exponential backoff, or two live sessions steal the stream back and
    forth every second."""
    return not replaced and lifetime_s >= threshold


def _advance_cursor(cursor_file: Path, cursor: str, *, accepted: bool) -> bool:
    """Persist a cursor only after the represented event is durably accepted."""
    if not cursor or not accepted:
        return False
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = cursor_file.with_suffix(cursor_file.suffix + ".tmp")
    temporary.write_text(cursor, encoding="utf-8")
    temporary.replace(cursor_file)
    return True


def _can_continue_after_delivery(proc, *, accepted: bool) -> bool:
    """Return false and close the stream when the contiguous cursor has a gap."""
    if accepted:
        return True
    log(
        "ef-stream",
        "Delivery was not durably accepted; reconnecting from the last "
        "contiguous cursor",
        level="warn",
    )
    try:
        if proc is not None and proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    return False


def _lark_send(text: str, user_id: str) -> bool:
    if not user_id or not text:
        return False
    text = linkify_bare_urls(text)
    try:
        r = subprocess.run(
            ["lark-cli", "im", "+messages-send",
             "--user-id", user_id, "--markdown", text, "--as", "bot"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _write_outbox(msg: str, metadata: dict, jarvis_dir: Path):
    ts = time.strftime("%Y-%m-%d %H:%M")
    try:
        text_json = json.dumps(msg, ensure_ascii=False)
        meta_json = json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    entry = f'{{"role":"assistant","text":{text_json},"ts":"{ts}","source":"eigenflux-stream","meta":{meta_json}}}\n'
    with open(jarvis_dir / "heartbeat_outbox.jsonl", "a") as f:
        f.write(entry)


def _deadletter_failed_send(jarvis_dir: Path, kind: str, text: str) -> None:
    """Record a failed Lark send as a dead-letter, not a delivery.

    Audit 2026-07-10: a failed _lark_send used to fall through to
    remember_seen + outbox + a "Delivered" log line — the dedup set then
    guaranteed the message could never be re-delivered while every ledger
    claimed success. The dead-letter row lets daemon.py (separate process,
    own notify channel) tell the user something was missed. Never raises.
    """
    log("ef-stream", f"Lark send failed — dead-lettering instead of "
        f"marking delivered ({kind})", level="warn")
    try:
        record_overdue(jarvis_dir, kind=kind, detail=(text or "")[:200],
                       due_since=now_local_str())
    except Exception as e:  # the stream loop must outlive any bookkeeping
        log("ef-stream", f"dead-letter write failed: {e}", level="warn")


def _deliver_and_mark(msg, ids, metadata, user_id, seen, seen_file, jd,
                      success_log: str = "Delivered real-time message"):
    """Submit one formatted event to the durable delivery pipeline.

    Once the pipeline has durably accepted the event, the upstream cursor may
    advance without risking loss. Only immediate delivery is mirrored to the
    conversation outbox; queued work remains honest until the pipeline flushes.
    """
    from core.delivery import (
        DeliveryEnvelope,
        TransportResult,
        deliver as deliver_envelope,
    )

    def transport(envelope, channel):
        if channel == "web":
            return TransportResult(True)
        ok = _lark_send(
            str(envelope.payload.get("text") or ""),
            user_id,
        )
        return TransportResult(bool(ok), error="" if ok else "lark transport failed")

    result = deliver_envelope(
        DeliveryEnvelope(
            source="eigenflux-stream",
            kind="text",
            payload={"text": msg},
            attention="notice",
            requested_channel="lark",
            urgent=True,
            dedup_key=(
                f"eigenflux-stream:{ids[0]}" if ids else ""
            ),
            matter_id=str((metadata or {}).get("matter_id") or ""),
            metadata={
                "external_event_ids": list(ids or []),
                "retry_existing": True,
            },
        ),
        root=jd,
        transport=transport,
    )
    if not result.accepted or result.state == "suppressed":
        _deadletter_failed_send(jd, "ef_stream_send_failed", msg)
        return seen, False
    seen = remember_seen(seen, ids)
    save_seen(seen_file, seen)
    if result.state == "delivered":
        _write_outbox(msg, metadata, jd)
        log("ef-stream", success_log)
    else:
        log(
            "ef-stream",
            f"Accepted real-time message into delivery queue ({result.state})",
        )
    return seen, True


def _deliver_memorial_and_mark(msg, ids, metadata, user_id, seen, seen_file, jd,
                               title: str) -> tuple[list, bool, bool]:
    """Deliver an EigenFlux event through the memorial card surface.

    Returns ``(seen, accepted, visible_now)``. ``accepted`` includes a card
    durably queued because the host is offline/inside quiet hours; once the
    intact card is on disk the upstream event can safely be marked seen.
    ``visible_now`` is false for queued cards, so follow-up analysis waits
    instead of commenting on a message Pascal has not received yet.
    """
    try:
        mid, _ = memorial.create(
            source="eigenflux", title=title, body=msg, preset="fyi",
            context=json.dumps(metadata or {}, ensure_ascii=False)[:1500],
            matter_id=str((metadata or {}).get("matter_id", "")),
        )
        state = memorial.get_memorial(mid) or {}
        delivery = state.get("delivery_status", "")
        accepted = memorial.delivery_accepted(state)
        if not accepted:
            _deadletter_failed_send(jd, "ef_stream_send_failed", msg)
            return seen, False, False
        seen = remember_seen(seen, ids)
        save_seen(seen_file, seen)
        log("ef-stream", f"Accepted {title} as memorial card ({delivery})")
        return seen, True, delivery == "delivered"
    except Exception as e:
        # The interaction adapter must never become a new message-loss mode.
        log("ef-stream", f"Memorial delivery failed ({e}); using legacy sender",
            level="warn")
        seen, accepted = _deliver_and_mark(
            msg, ids, metadata, user_id, seen, seen_file, jd)
        return seen, accepted, False


def _send_memorial_notice(title: str, body: str, user_id: str,
                          urgent: bool = False) -> bool:
    """Best-effort memorial notice with a legacy emergency fallback."""
    try:
        mid, _ = memorial.create(
            source="eigenflux", title=title, body=body, preset="fyi",
            urgent=urgent,
        )
        state = memorial.get_memorial(mid) or {}
        return memorial.delivery_accepted(state)
    except Exception as e:
        log("ef-stream", f"Memorial notice failed ({e}); using delivery fallback",
            level="warn")
        jd = Path(os.environ.get("JARVIS_DIR") or Path(__file__).parent.parent)
        _, accepted = _deliver_and_mark(
            body,
            [],
            {"kind": "notice"},
            user_id,
            [],
            jd / ".ef-notice-seen",
            jd,
            success_log=f"Delivered fallback notice: {title}",
        )
        return accepted


def _fetch_history(conv_id: str) -> str:
    """Prior turns of a conversation, so a reply isn't composed blind.

    Best-effort: returns "" on any failure (auth/timeout/parse).
    """
    if not conv_id:
        return ""
    try:
        h = subprocess.run(
            ["eigenflux", "msg", "history", "--conv-id", str(conv_id),
             "--limit", "10", "-f", "json"],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
        )
        if h.returncode != 0 or not h.stdout.strip():
            return ""
        data = json.loads(h.stdout)
        msgs = data.get("messages") or data.get("data", {}).get("messages") or []
        turns = [f"  {m.get('sender_name', '?')}: {m.get('content', '')}"
                 for m in msgs[-10:] if m.get("content")]
        if turns:
            return "Prior turns in this conversation (oldest→newest):\n" + "\n".join(turns)
    except Exception:
        pass
    return ""


def _run_analysis(
    detail: str,
    conv_id: str,
    jarvis_dir: str,
    log_file: str,
    procs: dict | None = None,
    stop_event: threading.Event | None = None,
):
    """Analyze an incoming message without granting external text local tools."""
    # Load friend list
    friends_ctx = ""
    contacts = list(Path.home().glob(".eigenflux/**/contacts.json"))
    if contacts:
        try:
            friends = json.loads(contacts[0].read_text())
            if isinstance(friends, list):
                names = [f"{f.get('agent_name','')} (remark: {f.get('remark','')})"
                         for f in friends[:20] if f.get("agent_name")]
                friends_ctx = "Known friends: " + ", ".join(names)
        except Exception:
            pass

    history_ctx = _fetch_history(conv_id)

    prompt = f"""[EIGENFLUX REAL-TIME MESSAGE — Quick Analysis]
A private message just arrived on EigenFlux:
{detail}

{history_ctx}

{friends_ctx}

Your note will ride on the SAME card as the raw message (原文+💡 一张卡). Your job:
1. Check memory: is this sender someone we know? What's our relationship?
2. If the message requires a response, suggest what to reply (brief, concrete) —
   use the prior turns above so the reply fits the conversation
3. If there's context the user needs (e.g. this relates to a current project), provide it

If the message is routine/no action needed, reply HEARTBEAT_OK.
Otherwise reply with a brief Chinese note (≤60 words) for the user.
若你给出了建议回复，在最后单独一行写按钮声明（每个标签=用户会打的那句话，≤14字）：
OPTIONS: 就按建议回复 | 先不回"""

    result = run_auxiliary_model(
        prompt,
        root=jarvis_dir,
        model="opus",
        timeout=120,
        allow_tools=False,
        process_holder=procs,
        process_key="analysis",
        cancelled=stop_event.is_set if stop_event is not None else None,
    )
    if not result.text:
        log(
            "ef-stream",
            "Message analysis exhausted provider chain: "
            + ",".join(result.attempted),
            level="warn",
        )
    return result.text


def run_loop(jarvis_dir: str, user_id: str = "", log_file: str = ""):
    jd = Path(jarvis_dir)
    # Cursor/seen state lives in the repo's eigenflux/ dir, NOT /tmp (REQ-57):
    # /tmp is wiped on reboot — the cursor was destroyed 3 times in 3 weeks,
    # causing PM re-delivery or gaps. One-time migration picks up a surviving
    # /tmp copy so an upgrade doesn't itself lose the position.
    state_dir = jd / "eigenflux"
    state_dir.mkdir(parents=True, exist_ok=True)
    cursor_file = state_dir / ".ef-cursor"
    seen_file = state_dir / ".ef-seen"
    for new, old in ((cursor_file, Path("/tmp/jarvis-ef-cursor")),
                     (seen_file, Path("/tmp/jarvis-ef-seen"))):
        if not new.exists() and old.exists():
            try:
                new.write_text(old.read_text())
                log("ef-stream", f"migrated {old} → {new}")
            except OSError:
                pass

    # Check eigenflux CLI
    if not shutil.which("eigenflux"):
        log("ef-stream", "eigenflux CLI not installed, skipping")
        return

    # Graceful shutdown: SIGTERM (bot.sh kill) / SIGINT set the stop flag and
    # terminate any in-flight children so we exit promptly without respawning.
    stop = threading.Event()
    procs: dict = {"stream": None, "analysis": None}

    def _shutdown(signum, _frame):
        stop.set()
        for key in ("analysis", "stream"):
            p = procs.get(key)
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        log("ef-stream", f"Signal {signum} received — shutting down gracefully")

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass  # not in main thread (e.g. under test) — skip handler install

    # Stall watchdog thread — see STALL_KILL_AFTER_S above. A killed child
    # EOFs the read loop below, so the existing respawn+backoff path takes
    # over; the timestamp reset ensures one kill per stall, not a kill storm.
    last_output = {"ts": time.monotonic()}

    def _stall_watchdog():
        while not stop.is_set():
            if stop.wait(STALL_POLL_S):
                return
            p = procs.get("stream")
            idle = time.monotonic() - last_output["ts"]
            if _is_stalled(p, idle):
                log("ef-stream", f"No stream output for {int(idle)}s — killing "
                    "stalled subprocess to force the reconnect path",
                    level="warn")
                last_output["ts"] = time.monotonic()
                try:
                    p.kill()
                except Exception:
                    pass

    threading.Thread(target=_stall_watchdog, daemon=True,
                     name="ef-stall-watchdog").start()

    if stop.wait(5):
        return
    log("ef-stream", "Starting real-time message stream")

    seen = load_seen(seen_file)
    backoff = 1
    max_backoff = 300
    failures = 0
    # Consecutive long-lived ZERO-OUTPUT connections. An idle day and an
    # up-but-mute outage (server accepts, delivers nothing) are protocol-
    # indistinguishable; immediate reconnect is right for both, but the streak
    # must stay visible or a multi-day mute outage reads as perfect health
    # (red-team catch on REQ-95).
    quiet_streak = 0

    while not stop.is_set():
        cmd = ["eigenflux", "stream", "-f", "json"]
        cursor = ""
        if cursor_file.exists():
            cursor = cursor_file.read_text().strip()
        if cursor:
            cmd.extend(["--cursor", cursor])

        log("ef-stream", f"Spawning: {' '.join(cmd)}")
        exit_code = -1
        proc = None
        replaced = False
        got_output = False
        conn_started = time.monotonic()

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=open(log_file, "a") if log_file else subprocess.DEVNULL,
                text=True,
            )
            procs["stream"] = proc
            last_output["ts"] = time.monotonic()

            for line in proc.stdout:
                last_output["ts"] = time.monotonic()
                got_output = True
                if stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue

                # "Connection replaced" comes on stdout — detect and break
                if "Connection replaced" in line:
                    log("ef-stream", "Connection replaced by another session — backing off")
                    replaced = True
                    proc.terminate()
                    break

                new_cursor = parse_cursor(line)

                # Friend-request / friend-accepted events ride a pm_push packet
                # with an empty `messages` array (or a separate friend_accepted
                # envelope), so the PM formatter below drops them. Handle them
                # here, before the empty-message skip, or they never surface.
                rel = format_relation_event(line)
                if rel:
                    rel_ids = extract_relation_ids(line)
                    if relation_event_kind(line) == "friend_request":
                        # The 10-minute eigenflux-friends task owns request
                        # review and execution. Sending here as well created a
                        # second, non-actionable card for the same request.
                        seen = remember_seen(seen, rel_ids)
                        save_seen(seen_file, seen)
                        log("ef-stream", "Friend request observed; lifecycle "
                            "delegated to eigenflux-friends")
                        _advance_cursor(
                            cursor_file, new_cursor, accepted=True
                        )
                        continue
                    if rel_ids and is_duplicate_event(rel_ids, set(seen)):
                        log("ef-stream", "Skipping already-delivered friend event (dedup)")
                        accepted = True
                    else:
                        seen, accepted, _ = _deliver_memorial_and_mark(
                            rel, rel_ids, {"kind": "relation"}, user_id,
                            seen, seen_file, jd, title="EigenFlux 好友动态")
                    _advance_cursor(
                        cursor_file, new_cursor, accepted=accepted
                    )
                    if not _can_continue_after_delivery(
                        proc, accepted=accepted
                    ):
                        break
                    continue

                # Format and deliver
                msg = format_message(line)
                if not msg:
                    _advance_cursor(cursor_file, new_cursor, accepted=True)
                    continue

                # Dedup: skip a re-delivered event (reconnect after a cursor-write gap)
                ids = extract_item_ids(line)
                if is_duplicate_event(ids, set(seen)):
                    log("ef-stream", "Skipping already-delivered message (dedup)")
                    _advance_cursor(cursor_file, new_cursor, accepted=True)
                    continue

                # ONE card per incoming message (7/22: the old raw-card-then-
                # analysis-card pair doubled every conversation into 2 pushes
                # — 12 cards in 32min during one chat). Analysis runs FIRST;
                # the single combined card carries 原文+💡. No-loss contract
                # is preserved because `seen` is only marked after the card
                # is accepted — a crash mid-analysis just re-delivers the
                # event on reconnect. Analysis failure degrades to a raw card.
                analysis = ""
                details = extract_detail(line)
                if details and not stop.is_set():
                    detail_str = "\n".join(json.dumps(d, ensure_ascii=False) for d in details)
                    conv_id = details[0].get("conv_id", "")
                    analysis = _run_analysis(
                        detail_str,
                        conv_id,
                        jarvis_dir,
                        log_file,
                        procs,
                        stop,
                    ) or ""
                body = msg
                if analysis and "HEARTBEAT_OK" not in analysis:
                    body = f"{msg}\n\n💡 {analysis}"
                m = re.search(r"\*\*(.+?)\*\*", msg)
                title = f"{m.group(1)} 来信" if m else "EigenFlux 消息"
                metadata = extract_metadata(line)
                # Attach network traffic to an existing Matter when explicit
                # metadata, conversation binding, or a strong lexical match is
                # available. Never auto-create Matters from ambient feed data.
                try:
                    from core.matter_router import ingest_signal
                    routed = ingest_signal({
                        "source_id": "eigenflux",
                        "source_type": "cli_stream",
                        "event_id": ids[0] if ids else new_cursor,
                        "title": title,
                        "summary": analysis,
                        "body": body,
                        "metadata": metadata,
                    })
                    metadata = {**metadata, **routed}
                except Exception as e:
                    log("ef-stream", f"Matter routing skipped: {e}", level="warn")
                seen, accepted, _ = _deliver_memorial_and_mark(
                    body, ids, metadata, user_id,
                    seen, seen_file, jd, title=title)
                if not _can_continue_after_delivery(
                    proc, accepted=accepted
                ):
                    break
                _advance_cursor(cursor_file, new_cursor, accepted=True)

                # Reset backoff on successful message
                backoff = 1
                failures = 0

            proc.wait(timeout=10)
            exit_code = proc.returncode

        except Exception as e:
            log("ef-stream", f"Stream error: {e}", level="warn")
            exit_code = -1
            try:
                if proc is not None:
                    proc.kill()
            except Exception:
                pass
        finally:
            procs["stream"] = None

        if stop.is_set():
            break

        if exit_code == 4:
            log("ef-stream", "Auth required — token may be expired", level="warn")
            _send_memorial_notice(
                "EigenFlux 需要重新登录",
                "EigenFlux token 已过期，请运行 `eigenflux auth login` 重新认证。",
                user_id, urgent=True)
            if stop.wait(300):
                break
            continue

        # REQ-95: a long-lived connection that dropped (stall-kill on a quiet
        # day, server restart) is healthy churn, not a failure — reconnect
        # immediately instead of letting the backoff ratchet to 300s forever.
        conn_lifetime = time.monotonic() - conn_started
        if _healthy_churn(conn_lifetime, replaced):
            failures = 0
            backoff = 1
            if got_output:
                quiet_streak = 0
                log("ef-stream",
                    f"Stream connection lived {int(conn_lifetime)}s before dropping "
                    "— treating as healthy churn, reconnecting immediately")
            else:
                quiet_streak += 1
                # Every 6th consecutive silent connection (~3h at the 30-min
                # stall cadence) escalates to warn: could be a quiet day,
                # could be an up-but-mute server — a human should decide.
                log("ef-stream",
                    f"Quiet stream reconnect #{quiet_streak} (lived "
                    f"{int(conn_lifetime)}s, zero output — idle stream or "
                    "up-but-mute outage)",
                    level="warn" if quiet_streak % 6 == 0 else "info",
                    expected=quiet_streak % 6 != 0)
            if stop.wait(1):
                break
            continue

        failures += 1
        log("ef-stream", f"Reconnecting in {backoff}s (failure #{failures})")
        if stop.wait(backoff):
            break
        backoff = min(backoff * 2, max_backoff)

    log("ef-stream", "Stream loop stopped")


if __name__ == "__main__":
    jarvis_dir = os.environ.get("JARVIS_DIR", ".")
    sys.path.insert(0, jarvis_dir)
    run_loop(
        jarvis_dir=jarvis_dir,
        user_id=os.environ.get("USER_ID", ""),
        log_file=os.environ.get("LOG_FILE", ""),
    )
