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
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
STAMP = ROOT / ".diag_last_alert.json"
DEDUP_WINDOW_S = 4 * 3600

# Raw substrings the proactive error gate kills (core/safety.py) — scrub them
# defensively even though our own composition shouldn't contain any.
_GATE_TRIGGERS = ("API Error", "Failed to authenticate", "Request not allowed")


def _user_id() -> str:
    """Lark user open_id from jarvis.yaml (the bot's owner)."""
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "jarvis.yaml").read_text()) or {}
        return (cfg.get("lark", {}) or {}).get("user_open_id", "") or ""
    except Exception:
        return ""


def _recently_alerted() -> bool:
    try:
        stamp = json.loads(STAMP.read_text())
        return time.time() - stamp.get("ts", 0) < DEDUP_WINDOW_S
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _mark_alerted(lines: list[str]) -> None:
    try:
        STAMP.write_text(json.dumps(
            {"ts": time.time(), "lines": lines[:20]}, ensure_ascii=False))
    except OSError:
        pass


def _send(text: str, user_id: str) -> bool:
    try:
        r = subprocess.run(
            ["lark-cli", "im", "+send", "--receive-id", user_id,
             "--receive-id-type", "open_id", "--msg-type", "text",
             "--content", json.dumps({"text": text}, ensure_ascii=False)],
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
        cache = ROOT / ".diag_last_pre.txt"
        if cache.exists():
            pre_output = cache.read_text(encoding="utf-8", errors="replace")

    warnings = extract_warnings(pre_output)
    if not warnings:
        return
    if _recently_alerted():
        print(f"[self_diagnostic_post] {len(warnings)} warning(s) suppressed "
              "(4h dedup window)", file=sys.stderr)
        return

    uid = _user_id()
    if not uid:
        print("[self_diagnostic_post] no user_open_id — cannot alert",
              file=sys.stderr)
        return

    text = ("🩺 自诊断发现 " + str(len(warnings)) + " 个问题：\n"
            + "\n".join(warnings[:12])
            + ("\n…（其余略）" if len(warnings) > 12 else "")
            + "\n（4 小时内同类告警只发一次）")
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
