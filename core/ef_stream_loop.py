#!/usr/bin/env python3
"""EigenFlux real-time stream loop — receives PMs and delivers to Lark.

Replaces the bash eigenflux_stream_loop() in bot.sh. Manages:
  - eigenflux stream subprocess with reconnect/backoff
  - Message formatting and Lark delivery
  - Outbox writing for main session visibility
  - Background Claude analysis for follow-up context

bot.sh launches this as: python3 -m core.ef_stream_loop &
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core.ef_stream import extract_detail, extract_metadata, format_message, parse_cursor
from core.log import log


def _lark_send(text: str, user_id: str) -> bool:
    if not user_id or not text:
        return False
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


def _run_analysis(detail: str, jarvis_dir: str, log_file: str):
    """Background Claude analysis of incoming message."""
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

    prompt = f"""[EIGENFLUX REAL-TIME MESSAGE — Quick Analysis]
A private message just arrived on EigenFlux:
{detail}

{friends_ctx}

The raw message was already forwarded to the user. Your job:
1. Check memory: is this sender someone we know? What's our relationship?
2. If the message requires a response, suggest what to reply (brief, concrete)
3. If there's context the user needs (e.g. this relates to a current project), provide it

If the message is routine/no action needed, reply HEARTBEAT_OK.
Otherwise reply with a brief Chinese note (≤60 words) for the user."""

    try:
        r = subprocess.run(
            ["claude", "--model", "sonnet", "--dangerously-skip-permissions",
             "--no-session-persistence", "--disable-slash-commands", "-p", prompt],
            capture_output=True, text=True, timeout=120,
            stdin=subprocess.DEVNULL,
        )
        return r.stdout.strip()
    except Exception as e:
        log("ef-stream", f"Claude analysis failed: {e}", level="warn")
        return ""


def run_loop(jarvis_dir: str, user_id: str = "", log_file: str = ""):
    jd = Path(jarvis_dir)
    cursor_file = Path("/tmp/jarvis-ef-cursor")

    # Check eigenflux CLI
    if not shutil.which("eigenflux"):
        log("ef-stream", "eigenflux CLI not installed, skipping")
        return

    time.sleep(5)
    log("ef-stream", "Starting real-time message stream")

    backoff = 1
    max_backoff = 60
    failures = 0
    max_failures = 20

    while True:
        # Kill leftover streams
        subprocess.run(["pkill", "-f", "eigenflux stream"],
                       capture_output=True, timeout=5)
        time.sleep(2)

        cmd = ["eigenflux", "stream", "-f", "json"]
        cursor = ""
        if cursor_file.exists():
            cursor = cursor_file.read_text().strip()
        if cursor:
            cmd.extend(["--cursor", cursor])

        log("ef-stream", f"Spawning: {' '.join(cmd)}")

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=open(log_file, "a") if log_file else subprocess.DEVNULL,
                text=True,
            )

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                # Extract cursor
                new_cursor = parse_cursor(line)
                if new_cursor:
                    cursor_file.write_text(new_cursor)

                # Format and deliver
                msg = format_message(line)
                if not msg:
                    continue

                _lark_send(msg, user_id)
                metadata = extract_metadata(line)
                _write_outbox(msg, metadata, jd)
                log("ef-stream", "Delivered real-time message")

                # Background analysis
                details = extract_detail(line)
                if details:
                    detail_str = "\n".join(json.dumps(d, ensure_ascii=False) for d in details)
                    analysis = _run_analysis(detail_str, jarvis_dir, log_file)
                    if analysis and "HEARTBEAT_OK" not in analysis:
                        _lark_send(f"💡 {analysis}", user_id)
                        log("ef-stream", "Follow-up analysis sent")

                backoff = 1
                failures = 0

            proc.wait()
            exit_code = proc.returncode

        except Exception as e:
            log("ef-stream", f"Stream error: {e}", level="warn")
            exit_code = -1

        if exit_code == 4:
            log("ef-stream", "Auth required — token may be expired", level="warn")
            _lark_send("⚠️ EigenFlux token expired. Please re-authenticate: `eigenflux auth login`", user_id)
            time.sleep(300)
            continue

        failures += 1
        if failures >= max_failures:
            log("ef-stream", f"{max_failures} consecutive failures, backing off to 5min", level="warn")
            time.sleep(300)
            failures = 0
            backoff = 1
            continue

        log("ef-stream", f"Reconnecting in {backoff}s (failure #{failures})")
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    jarvis_dir = os.environ.get("JARVIS_DIR", ".")
    sys.path.insert(0, jarvis_dir)
    run_loop(
        jarvis_dir=jarvis_dir,
        user_id=os.environ.get("USER_ID", ""),
        log_file=os.environ.get("LOG_FILE", ""),
    )
