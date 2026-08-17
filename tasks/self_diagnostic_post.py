#!/usr/bin/env python3
"""self_diagnostic_post.py — deterministic alert path for self-diagnostic.

REQ-39: self-diagnostic sits in SILENT_TASKS (its LLM summary is housekeeping
noise), but that gate also muted the very alerts its prompt promises — the
system's only health alarm was structurally silent (the dashboard died for
23 days unseen). Detection and delivery are now SPLIT: this post-script scans
the PRE output (passed through by the runner alongside Claude's reply on
stdin is the LLM summary — we re-read the pre data from the freshest source)
for ⚠️ lines and, when any exist, sends ONE aggregated Lark alert directly
via lark-cli, bypassing both SILENT_TASKS (a post side effect is not a
user_message) and the proactive error gate (we compose the text ourselves,
in Chinese, with no raw error strings).

Dedup: at most one alert per 4h window (.diag_last_alert.json stamp) so a
persistent condition does not nag every cycle. The LLM summary on stdin is
still swallowed (stays silent) — only the deterministic ⚠️ extraction talks.

No LLM, no heuristics: if the pre printed ⚠️, Pascal hears about it.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR") or CODE_ROOT)
# The heartbeat runs this as a script, so sys.path[0] is tasks/ — not the repo
# root. Without this insert `from core import memorial` raises
# ModuleNotFoundError and the alarm silently degrades to the plain-text
# emergency path (observed 2026-07-27). Every other task script that imports
# core already does this; this one was the sole outlier.
sys.path.insert(0, str(CODE_ROOT))
STAMP = JARVIS_DIR / ".diag_last_alert.json"
DEDUP_WINDOW_S = 4 * 3600

# Raw substrings the proactive error gate kills (core/safety.py) — scrub them
# defensively even though our own composition shouldn't contain any.
_GATE_TRIGGERS = ("API Error", "Failed to authenticate", "Request not allowed")


def _user_id() -> str:
    """Lark user open_id from jarvis.yaml (the bot's owner)."""
    try:
        import yaml
        cfg = yaml.safe_load((JARVIS_DIR / "jarvis.yaml").read_text()) or {}
        lark = cfg.get("lark", {}) or {}
        # Production config and bot.sh use `user_id`; older installs used
        # `user_open_id`. Accept both so the independent diagnostic alarm
        # cannot silently disable itself over a naming drift.
        return lark.get("user_id", "") or lark.get("user_open_id", "") or ""
    except Exception:
        return ""


REMIND_INTERVAL_S = 24 * 3600


def _read_stamp() -> dict:
    try:
        return json.loads(STAMP.read_text()) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def should_alert(warnings: list[str], stamp: dict, now: float | None = None) -> bool:
    """Alert on NEW content, not on window lapse (REQ-109).

    The old time-only window produced an 8h relay ping-pong: the daemon's
    re-send wrote the shared stamp, this post then suppressed on the fresh
    stamp, the daemon's window lapsed first and it "rescued" again — Pascal
    got the same persisting warning every 8 hours, each blaming a "broken"
    primary path. Rules: 4h hard floor stays; past it, send only if a warning
    line is not already in the stamped set; an unchanged set re-alerts at
    most once per 24h as a reminder.
    """
    if not warnings:
        return False
    now = time.time() if now is None else now
    age = now - float(stamp.get("ts", 0) or 0)
    if age < DEDUP_WINDOW_S:
        return False
    # Compare exactly what the stamp stores (first 20 lines): with >20
    # warnings a full-set comparison is永远 non-empty and re-alerts every 4h
    # forever (red-team 7/20 finding #7).
    if set(warnings[:20]) - set(stamp.get("lines") or []):
        return True
    return age >= REMIND_INTERVAL_S


def _mark_alerted(lines: list[str]) -> None:
    try:
        tmp = STAMP.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"ts": time.time(), "lines": lines[:20]}, ensure_ascii=False))
        os.replace(tmp, STAMP)
    except OSError:
        pass


# Warning line emitted by self_diagnostic_pre.sh when the lark-cli user
# token is gone. Seeing it means the card can offer a REAL fix button —
# 「现在授权」runs the device flow instead of asking Pascal to open a
# terminal (2026-08-07: the reply-only version of this button was a dead
# end he had to talk his way out of).
_USER_TOKEN_MARKER = "user token 探针失败"

_AUTH_OPTIONS = [
    {"key": "auth", "label": "现在授权",
     "action": {"type": "lark_auth_login", "params": {}}},
    {"key": "read", "label": "已阅", "action": None},
]


def _options_for(text: str) -> list[dict] | None:
    """Real fix buttons for warnings that have a hands-free fix."""
    if _USER_TOKEN_MARKER in text:
        return [dict(o) for o in _AUTH_OPTIONS]
    return None


def _send(text: str, user_id: str) -> bool:
    try:
        from core import memorial
        mid, _ = memorial.create(
            source="selfmon", title="自诊断发现问题", body=text,
            options=_options_for(text), preset="fyi", urgent=True,
            attention="alert")
        state = memorial.get_memorial(mid) or {}
        if state.get("delivery_status") in {
                "delivered", "queued", "retry_queued"}:
            return True
    except Exception as e:
        print(f"[self_diagnostic_post] memorial send failed: {e}",
              file=sys.stderr)
    # Emergency fallback: if memorial imports/storage itself is broken, keep
    # the old independent plain-text path and then the macOS local alert.
    try:
        from core.lark_bot_transport import send as send_as_bot

        direct = send_as_bot(text=text, user_id=user_id)
        if direct.attempted:
            return direct.ok
        # --as bot: without it lark-cli falls back to the USER identity, which
        # fails on installs that never ran `lark-cli auth login` — exactly the
        # machines where this emergency path matters (daemon.py's own sends
        # always pass --as bot; 2026-07-13 the collaborator's first-install
        # alert only got out because the daemon re-delivered it).
        r = subprocess.run(
            ["lark-cli", "im", "+send", "--receive-id", user_id,
             "--receive-id-type", "open_id", "--msg-type", "text",
             "--content", json.dumps({"text": text}, ensure_ascii=False),
             "--as", "bot"],
            capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception as e:
        print(f"[self_diagnostic_post] lark send failed: {e}", file=sys.stderr)
        return False


def extract_warnings(pre_output: str) -> list[str]:
    """Deterministic ⚠️ extraction from the pre-script's health report."""
    out = []
    for ln in pre_output.splitlines():
        s = ln.strip()
        if s.startswith("⚠️") or s.startswith("⚠"):
            for trig in _GATE_TRIGGERS:
                s = s.replace(trig, "[已脱敏错误串]")
            out.append(s)
    return out


def main():
    # stdin carries Claude's (silent) summary — read and discard; the
    # deterministic signal is the pre-script's own ⚠️ lines, which the runner
    # leaves on disk for us via the freshest pre re-run below. Re-running the
    # pre would double its cost, so the runner is asked to pass pre data
    # through the DIAG_PRE_FILE env hop instead.
    sys.stdin.read()

    import os
    pre_file = os.environ.get("DIAG_PRE_FILE", "")
    pre_output = ""
    if pre_file and Path(pre_file).exists():
        pre_output = Path(pre_file).read_text(encoding="utf-8", errors="replace")
    else:
        # Fallback: the runner persists the last diagnostic pre output here.
        cache = JARVIS_DIR / ".diag_last_pre.txt"
        if cache.exists():
            pre_output = cache.read_text(encoding="utf-8", errors="replace")

    warnings = extract_warnings(pre_output)
    stamp = _read_stamp()
    if not should_alert(warnings, stamp):
        if warnings:
            print(f"[self_diagnostic_post] {len(warnings)} warning(s) "
                  "suppressed (no new content)", file=sys.stderr)
        return

    uid = _user_id()
    if not uid:
        print("[self_diagnostic_post] no user_open_id — cannot alert",
              file=sys.stderr)
        return

    unchanged = not (set(warnings[:20]) - set(stamp.get("lines") or []))
    text = ("🩺 自诊断发现 " + str(len(warnings)) + " 个问题"
            + ("（昨天说过的还没好）" if unchanged else "") + "：\n"
            + "\n".join(warnings[:12])
            + ("\n…（其余略）" if len(warnings) > 12 else "")
            + "\n（同样的问题一天最多提醒一次）")
    if _send(text, uid):
        _mark_alerted(warnings)
        print(f"[self_diagnostic_post] alerted {len(warnings)} warning(s)",
              file=sys.stderr)
    else:
        # Last-resort local signal: lark-cli itself may be the broken part.
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "自诊断: {len(warnings)} 个问题，Lark 告警发送失败" '
                 'with title "Jarvis"'],
                capture_output=True, timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    main()
