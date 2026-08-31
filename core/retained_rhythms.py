"""Explicit owner subscriptions for Jarvis-initiated companion rhythms.

Silence is healthy by default. A recurring companion card is eligible only
when the private ``jarvis.yaml`` contains an exact boolean ``true`` for that
rhythm, and no more than two rhythms may be retained at once.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from core.config import Config


RHYTHMS = ("checkin", "daily_reflect", "exercise_week")
ALIASES = {
    "daily-reflect": "daily_reflect",
    "exercise-week": "exercise_week",
}
MAX_RETAINED_RHYTHMS = 2


def normalize(name: object) -> str:
    value = str(name or "").strip().lower()
    return ALIASES.get(value, value)


def configured(root: str | Path | None = None) -> dict[str, bool]:
    base = Path(root or os.environ.get("JARVIS_DIR") or Path.cwd())
    raw = Config(base / "jarvis.yaml").get("retained_rhythms", {})
    values = raw if isinstance(raw, dict) else {}
    return {name: values.get(name) is True for name in RHYTHMS}


def validation_error(root: str | Path | None = None) -> str:
    enabled = [name for name, value in configured(root).items() if value]
    if len(enabled) > MAX_RETAINED_RHYTHMS:
        return (
            f"at most {MAX_RETAINED_RHYTHMS} retained rhythms may be enabled"
        )
    return ""


def is_enabled(name: object, root: str | Path | None = None) -> bool:
    rhythm = normalize(name)
    if rhythm not in RHYTHMS or validation_error(root):
        return False
    return configured(root)[rhythm]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="core.retained_rhythms")
    sub = parser.add_subparsers(dest="command", required=True)
    enabled = sub.add_parser("enabled")
    enabled.add_argument("rhythm", choices=(*RHYTHMS, *ALIASES))
    sub.add_parser("status")
    args = parser.parse_args(argv)

    error = validation_error()
    if args.command == "enabled":
        return 0 if not error and is_enabled(args.rhythm) else 1

    values = configured()
    print("Retained rhythms (explicit private subscriptions):")
    for name in RHYTHMS:
        print(f"  {name}: {'enabled' if values[name] else 'disabled'}")
    if error:
        print(f"ERROR: {error}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
