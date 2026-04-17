#!/usr/bin/env python3
"""Post-hook: submit feedback to EigenFlux via CLI, output user message to Lark."""
import json
import os
import re
import subprocess
import sys
import traceback

LOG = open(os.environ.get("LOG_FILE", os.devnull), "a")
PATH = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")


def run_eigenflux(*args: str, stdin_data: str | None = None) -> dict:
    result = subprocess.run(
        ["eigenflux", *args, "-f", "json"],
        capture_output=True, text=True,
        env={**os.environ, "PATH": PATH},
        input=stdin_data,
    )
    if result.returncode != 0:
        print(f"[eigenflux-feed] CLI error: {result.stderr.strip()}", file=LOG)
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0

    raw = re.sub(r'^```json?\s*', '', raw)
    raw = re.sub(r'```\s*$', '', raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[eigenflux-feed] JSON parse failed: {e}", file=LOG)
        return 0

    # Submit feedback scores via CLI
    fb = data.get("feedback", [])
    if fb:
        items = []
        for i in fb:
            try:
                items.append({"item_id": int(i["item_id"]), "score": int(i["score"])})
            except (ValueError, KeyError, TypeError) as e:
                print(f"[eigenflux-feed] bad feedback entry {i!r}: {e}", file=LOG)
        if items:
            try:
                resp = run_eigenflux("feed", "feedback", "--items", json.dumps(items))
                print(f"[eigenflux-feed] {len(items)} items scored", file=LOG)
            except Exception:
                print("[eigenflux-feed] feedback submission failed:", file=LOG)
                traceback.print_exc(file=LOG)

    # Output user message (this becomes the Lark reply)
    msg = str(data.get("user_message", "")).strip()
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
