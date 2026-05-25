#!/usr/bin/env python3
"""Post-hook: publish to EigenFlux via CLI if Claude decided to."""
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card

LOG = open(os.environ.get("LOG_FILE", os.devnull), "a")
PATH_ENV = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0

    raw = re.sub(r'^```json?\s*', '', raw)
    raw = re.sub(r'```\s*$', '', raw)

    try:
        data = json.loads(extract_json(raw))
    except json.JSONDecodeError as e:
        print(f"[eigenflux-publish] JSON parse failed: {e}", file=LOG)
        return 0

    if not data.get("should_publish"):
        return 0

    content = data.get("content")
    notes = data.get("notes")
    if not content or not notes:
        print("[eigenflux-publish] missing content or notes", file=LOG)
        return 0

    notes_str = json.dumps(notes) if isinstance(notes, dict) else str(notes)
    url = data.get("url", "")

    cmd = ["eigenflux", "publish",
           "--content", content,
           "--notes", notes_str,
           "--accept-reply",
           "-f", "json"]
    if url:
        cmd.extend(["--url", url])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            env={**os.environ, "PATH": PATH_ENV},
        )
        if result.returncode == 0:
            print("[eigenflux-publish] published successfully", file=LOG)
            # Update local publish state for cooldown tracking
            state_file = JARVIS_DIR / "eigenflux" / "publish_state.json"
            now_epoch = int(time.time())
            # Load existing state to preserve history
            state: dict = {}
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            state["last_publish_epoch"] = now_epoch
            # Append to recent history (keep last 20)
            history = state.get("recent", [])
            summary = (notes.get("summary", "") if isinstance(notes, dict)
                       else "")
            history.append({
                "epoch": now_epoch,
                "summary": summary,
                "content_preview": content[:120],
            })
            state["recent"] = history[-20:]
            from core.safety import atomic_write
            atomic_write(state_file, json.dumps(state, ensure_ascii=False))
            # Notify user via card (full content — broadcasts are short by design)
            print(build_card("📡 EigenFlux · 广播", f"已发布广播:\n\n{content}"))
        else:
            print(f"[eigenflux-publish] CLI error: {result.stderr.strip()}", file=LOG)
    except Exception:
        print("[eigenflux-publish] publish raised:", file=LOG)
        traceback.print_exc(file=LOG)

    return 0


if __name__ == "__main__":
    sys.exit(main())
