"""Reliable user-reply delivery in bot.sh (audit 2026-07-10, survivors[4]).

The conversation channel used to be a single lark-cli attempt: a transient
network failure at send time silently discarded the finished $reply — the
exact REQ-11 pain, previously fixed only on the heartbeat channel
(core/heartbeat_loop.py: SEND_RETRY_DELAYS=(2,5) + dead-letter). bot.sh now
mirrors those semantics for the reply path: per-attempt timeout (lark-cli has
no socket timeout, so a half-open connection could wedge the handler subshell
forever), (2,5)s backoff retries, then a core.delivery_deadletter row
(kind=reply_send_failed) so daemon.py's independent process can retry and
retain evidence until the shared Lark channel accepts the notice.

The fix lives in shell, so this file combines static-source wiring guards
(the test_restart_loop_regression.py approach) with behavioral tests that
extract the new functions from bot.sh and run them against a mocked
lark_reply/lark_send. No real lark-cli, no network, all writes under tmp_path.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_SH = (ROOT / "bot.sh").read_text()

# Mocks stand in for the plugin senders (plugins/lark/client.sh). The attempt
# counter is file-based because each attempt runs inside a with_fn_timeout
# background subshell — a shell variable would not survive it.
_MOCKS = r"""
set -uo pipefail
log_warn() { echo "[WARN] $*" >> "$LOG_FILE"; }
log_err()  { echo "[ERROR] $*" >> "$LOG_FILE"; }
# lark-cli stand-in: fails the first $FAIL_FIRST_N calls, then succeeds.
lark_reply() {
  local n
  n=$(cat "$CNT_FILE" 2>/dev/null || echo 0)
  n=$((n + 1))
  printf '%s' "$n" > "$CNT_FILE"
  [ "$n" -gt "${FAIL_FIRST_N:-0}" ]
}
lark_send() { lark_reply "-" "$1"; }
"""


def _extract_fn(name: str) -> str:
    """Pull one top-level `name() { ... }` block out of bot.sh."""
    m = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}", BOT_SH)
    assert m, f"function {name}() not found in bot.sh"
    return m.group(0) + "\n"


def _script(*fns: str, extra: str = "") -> str:
    return _MOCKS + "".join(_extract_fn(f) for f in fns) + extra + '\n"$@"\n'


def _run(tmp_path, script, args, extra_env=None):
    env = dict(os.environ)
    env.update({
        "JARVIS_DIR": str(tmp_path),   # dead-letter root → tmp, never the repo
        "PYTHONPATH": str(ROOT),       # so `from core...` still resolves
        "LOG_FILE": str(tmp_path / "log.txt"),
        "CNT_FILE": str(tmp_path / "attempts"),
        "LARK_SEND_TIMEOUT": "30",
    })
    env.update(extra_env or {})
    sh = tmp_path / "harness.sh"
    sh.write_text(script)
    return subprocess.run(["bash", str(sh)] + list(args),
                          capture_output=True, text=True, env=env, timeout=60)


def _deadletter_rows(tmp_path):
    path = tmp_path / "data" / ".delivery_deadletter.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ── Static wiring guards ─────────────────────────────────────────────


def test_reply_path_uses_reliable_sender():
    """The live reply path terminates at the shared Python pipeline."""
    assert 'if ! lark_reply "$message_id" "$reply"' not in BOT_SH
    assert 'delivery_reply_reliable "$message_id" "$reply"' in BOT_SH
    assert "python3 -m core.delivery send" in _extract_fn(
        "delivery_reply_reliable")
    assert '_answer_provider="Claude backup2"' in BOT_SH
    assert '_answer_provider="Codex"' in BOT_SH


def test_send_to_lark_uses_reliable_sender():
    assert "delivery_send_reliable" in _extract_fn("send_to_lark")


def test_production_bot_cards_use_unified_delivery_sender():
    assert 'delivery_card_reliable "$_bg_start_card"' in BOT_SH
    assert 'delivery_card_reliable "$card_json"' in BOT_SH
    assert "lark_send_card \"$_bg_start_card\"" not in BOT_SH
    assert 'lark_send_card "$card_json"' not in BOT_SH


def test_queued_cards_do_not_fall_back_to_duplicate_plain_text():
    card_sender = _extract_fn("delivery_card_reliable")
    notice_sender = _extract_fn("delivery_send_reliable")

    assert "queued|attempting|delivered|read|acted|suppressed" in card_sender
    assert "queued|attempting|delivered|read|acted|suppressed" in notice_sender


def test_backoff_mirrors_heartbeat_send_retry_delays():
    """(2,5)s — same schedule as core/heartbeat_loop.py SEND_RETRY_DELAYS."""
    assert BOT_SH.count("for _delay in 2 5") == 2  # reply + send wrappers


def test_deadletter_kind_is_reply_send_failed():
    assert "reply_send_failed" in BOT_SH


# ── Behavioral: retry / dead-letter / timeout ────────────────────────


def test_reply_retries_then_succeeds(tmp_path):
    script = _script("with_fn_timeout", "_deadletter_reply", "lark_reply_reliable")
    r = _run(tmp_path, script, ["lark_reply_reliable", "om_test", "hello"],
             extra_env={"FAIL_FIRST_N": "1"})
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "attempts").read_text() == "2"
    assert _deadletter_rows(tmp_path) == []  # delivered → nothing to page
    assert "retrying in 2s" in (tmp_path / "log.txt").read_text()


def test_reply_dead_letters_after_all_retries(tmp_path):
    script = _script("with_fn_timeout", "_deadletter_reply", "lark_reply_reliable")
    reply = "第一行结论\n第二行细节 " + "x" * 300
    r = _run(tmp_path, script, ["lark_reply_reliable", "om_dead", reply],
             extra_env={"FAIL_FIRST_N": "99"})
    assert r.returncode == 1
    assert (tmp_path / "attempts").read_text() == "3"  # 1 try + 2 retries
    rows = _deadletter_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "reply_send_failed"
    assert "mid=om_dead" in rows[0]["detail"]
    # head of the lost reply survives, newlines collapsed for the jsonl row
    assert "第一行结论 第二行细节" in rows[0]["detail"]
    assert rows[0]["due_since"]


def test_send_reliable_succeeds_first_try(tmp_path):
    script = _script("with_fn_timeout", "_deadletter_reply", "lark_send_reliable")
    r = _run(tmp_path, script, ["lark_send_reliable", "ping"])
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "attempts").read_text() == "1"
    assert _deadletter_rows(tmp_path) == []


def test_deadletter_without_message_id_uses_dash(tmp_path):
    """lark_send_reliable has no message_id — the row must still be parseable."""
    script = _script("_deadletter_reply")
    r = _run(tmp_path, script, ["_deadletter_reply", "", "night owl"])
    assert r.returncode == 0, r.stderr
    rows = _deadletter_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["detail"].startswith("mid=- ")
    assert "night owl" in rows[0]["detail"]


def test_with_fn_timeout_kills_hung_sender(tmp_path):
    """A half-open connection (sender never returns) must not wedge the
    handler subshell — the attempt is killed at the deadline and reads as a
    failure, which feeds the retry/dead-letter path."""
    script = _script("with_fn_timeout",
                     extra="hang() { command sleep 30 >/dev/null 2>&1; }\n")
    t0 = time.monotonic()
    r = _run(tmp_path, script, ["with_fn_timeout", "1", "hang"])
    elapsed = time.monotonic() - t0
    assert r.returncode != 0
    assert elapsed < 10, f"hung sender not killed in time ({elapsed:.1f}s)"
