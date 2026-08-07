"""Standing self-improvement cycle — value-driven, quiet, every three days.

Owner authorization (2026-08-07): 「你可以自己定时每几天根据你给我提供的价值，
进行进步」, on top of「有些自进化不用打扰我哦」. So: every ~3 days a detached
Claude Code session runs one full self-improve round, mining its topics from
the real value ledgers (批阅率, noise sources, dead ends, presence) and
shipping internal reversible improvements without pinging Pascal; only
directional or irreversible choices surface as a card.

The heartbeat hosts the SCHEDULE only: the pre-hook gates on a 3-day stamp,
spawns the detached session, and prints nothing — so the cycle consumes zero
heartbeat model budget and cannot starve other tasks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

JARVIS_DIR = Path(os.environ.get(
    "JARVIS_DIR", Path(__file__).resolve().parent.parent))
# The session's cwd decides which auto-memory it loads. The repos directory
# maps to the memory that carries the self-improve workflow, the quiet rule,
# and every standing feedback contract — that context IS the guardrail.
WORK_DIR = Path(os.environ.get(
    "JV_SELF_IMPROVE_CWD", Path.home() / "Desktop" / "jarvis" / "repos"))

CYCLE_S = 3 * 86400
PROMPT_FILE = "scripts/self_improve_prompt.md"
LOG_FILE = "/tmp/jarvis-self-improve.log"


def _state_path() -> Path:
    return JARVIS_DIR / "data" / "self_improve_cycle.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def due(now_epoch: float | None = None) -> bool:
    """One live round at a time, at most one spawn per CYCLE_S."""
    now = time.time() if now_epoch is None else float(now_epoch)
    state = _read_state()
    if _pid_alive(int(state.get("pid") or 0)):
        return False
    return now - float(state.get("spawned_at") or 0) >= CYCLE_S


def spawn(popen=subprocess.Popen, now_epoch: float | None = None) -> int:
    """Detach one self-improve session; stamp first so a crash can't loop.

    Returns the pid (0 when the prompt file is missing — a deploy-drift
    guard: an empty prompt would burn a full session on nothing).
    """
    prompt_path = JARVIS_DIR / PROMPT_FILE
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"self-improve-cycle: prompt missing: {prompt_path}",
              file=sys.stderr)
        return 0
    if not prompt:
        return 0

    from core.claude_bin import resolve_claude_bin
    now = time.time() if now_epoch is None else float(now_epoch)
    log = open(LOG_FILE, "a")
    log.write(f"\n===== self-improve cycle spawned at {time.ctime(now)} =====\n")
    proc = popen(
        [resolve_claude_bin(), "--dangerously-skip-permissions", "-p", prompt],
        cwd=str(WORK_DIR), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True)
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(
        {"spawned_at": now, "pid": int(getattr(proc, "pid", 0) or 0)},
        ensure_ascii=False), encoding="utf-8")
    return int(getattr(proc, "pid", 0) or 0)


def main(argv: list[str]) -> int:
    if argv[:1] == ["tick"]:
        if due():
            pid = spawn()
            if pid:
                print(f"self-improve-cycle: spawned pid {pid}",
                      file=sys.stderr)
        return 0
    print("usage: python3 -m core.self_improve_cycle tick", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
