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
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.card import linkify_bare_urls
from core.claude_bin import resolve_claude_bin
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
    remember_seen,
    save_seen,
)
from core.log import log


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


def _run_analysis(detail: str, conv_id: str, jarvis_dir: str, log_file: str, procs: dict | None = None):
    """Background Claude analysis of an incoming message, with history context."""
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

The raw message was already forwarded to the user. Your job:
1. Check memory: is this sender someone we know? What's our relationship?
2. If the message requires a response, suggest what to reply (brief, concrete) —
   use the prior turns above so the reply fits the conversation
3. If there's context the user needs (e.g. this relates to a current project), provide it

If the message is routine/no action needed, reply HEARTBEAT_OK.
Otherwise reply with a brief Chinese note (≤60 words) for the user."""

    try:
        p = subprocess.Popen(
            [resolve_claude_bin(), "--model", "opus", "--dangerously-skip-permissions",
             "--no-session-persistence", "--disable-slash-commands", "-p", prompt],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            stdin=subprocess.DEVNULL,
        )
        if procs is not None:
            procs["analysis"] = p
        try:
            out, _ = p.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            out = ""
        return (out or "").strip()
    except Exception as e:
        log("ef-stream", f"Claude analysis failed: {e}", level="warn")
        return ""
    finally:
        if procs is not None:
            procs["analysis"] = None


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

    if stop.wait(5):
        return
    log("ef-stream", "Starting real-time message stream")

    seen = load_seen(seen_file)
    backoff = 1
    max_backoff = 300
    failures = 0

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

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=open(log_file, "a") if log_file else subprocess.DEVNULL,
                text=True,
            )
            procs["stream"] = proc

            for line in proc.stdout:
                if stop.is_set():
                    break
                line = line.strip()
                if not line:
                    continue

                # "Connection replaced" comes on stdout — detect and break
                if "Connection replaced" in line:
                    log("ef-stream", "Connection replaced by another session — backing off")
                    proc.terminate()
                    break

                # Advance cursor first (even on a duplicate, so we move past it)
                new_cursor = parse_cursor(line)
                if new_cursor:
                    cursor_file.write_text(new_cursor)

                # Friend-request / friend-accepted events ride a pm_push packet
                # with an empty `messages` array (or a separate friend_accepted
                # envelope), so the PM formatter below drops them. Handle them
                # here, before the empty-message skip, or they never surface.
                rel = format_relation_event(line)
                if rel:
                    rel_ids = extract_relation_ids(line)
                    if rel_ids and is_duplicate_event(rel_ids, set(seen)):
                        log("ef-stream", "Skipping already-delivered friend event (dedup)")
                    else:
                        _lark_send(rel, user_id)
                        _write_outbox(rel, {"kind": "relation"}, jd)
                        seen = remember_seen(seen, rel_ids)
                        save_seen(seen_file, seen)
                        log("ef-stream", "Delivered friend-request/relation event")
                    continue

                # Format and deliver
                msg = format_message(line)
                if not msg:
                    continue

                # Dedup: skip a re-delivered event (reconnect after a cursor-write gap)
                ids = extract_item_ids(line)
                if is_duplicate_event(ids, set(seen)):
                    log("ef-stream", "Skipping already-delivered message (dedup)")
                    continue

                _lark_send(msg, user_id)
                metadata = extract_metadata(line)
                _write_outbox(msg, metadata, jd)
                log("ef-stream", "Delivered real-time message")

                seen = remember_seen(seen, ids)
                save_seen(seen_file, seen)

                # Background analysis (skip if shutting down)
                details = extract_detail(line)
                if details and not stop.is_set():
                    detail_str = "\n".join(json.dumps(d, ensure_ascii=False) for d in details)
                    conv_id = details[0].get("conv_id", "")
                    analysis = _run_analysis(detail_str, conv_id, jarvis_dir, log_file, procs)
                    if analysis and "HEARTBEAT_OK" not in analysis:
                        _lark_send(f"💡 {analysis}", user_id)
                        log("ef-stream", "Follow-up analysis sent")

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
            _lark_send("⚠️ EigenFlux token expired. Please re-authenticate: `eigenflux auth login`", user_id)
            if stop.wait(300):
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
