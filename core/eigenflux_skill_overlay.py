"""Deterministically compose upstream EigenFlux skills with Jarvis contracts."""

from __future__ import annotations

import argparse
from pathlib import Path


BEGIN = "<!-- JARVIS-LOCAL-OVERLAY:BEGIN -->"
END = "<!-- JARVIS-LOCAL-OVERLAY:END -->"
DEFAULT_ANCHOR = "### Fetch Unread Messages"
UPSTREAM_DIRECT_SEND = '''# Direct message to a friend
eigenflux msg send --content "YOUR MESSAGE" --receiver-id FRIEND_AGENT_ID'''
JARVIS_DIRECT_SEND = '''# Direct message to a friend in Jarvis (verified target + read-back)
python3 -m core.eigenflux_messages send \\
  --recipient "EXACT FRIEND NAME OR REMARK" \\
  --content "YOUR MESSAGE"'''


def render(base: str, overlay: str, *, anchor: str = DEFAULT_ANCHOR) -> str:
    """Insert one replaceable local block before a stable upstream heading."""
    clean = str(base)
    if clean.count(BEGIN) != clean.count(END):
        raise ValueError("incomplete Jarvis overlay marker pair")
    while BEGIN in clean and END in clean:
        before, rest = clean.split(BEGIN, 1)
        _, after = rest.split(END, 1)
        clean = before.rstrip() + "\n\n" + after.lstrip()
    clean = clean.replace(UPSTREAM_DIRECT_SEND, JARVIS_DIRECT_SEND)
    block = f"{BEGIN}\n{str(overlay).strip()}\n{END}"
    if anchor in clean:
        prefix, suffix = clean.split(anchor, 1)
        return (
            prefix.rstrip()
            + "\n\n"
            + block
            + "\n\n"
            + anchor
            + suffix
        )
    return clean.rstrip() + "\n\n" + block + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--overlay", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = render(
        Path(args.base).read_text(encoding="utf-8"),
        Path(args.overlay).read_text(encoding="utf-8"),
    )
    Path(args.output).write_text(result, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
