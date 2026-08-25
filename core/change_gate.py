"""Small privacy-preserving change gates for deterministic maintenance tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from core.safety import atomic_write


def tree_signature(root: str | Path, *, exclude: tuple[str, ...] = ()) -> str:
    base = Path(root)
    excluded = set(exclude)
    rows: list[str] = []
    if not base.is_dir():
        return ""
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.name in excluded:
            continue
        try:
            stat = path.stat()
            relative = path.relative_to(base)
        except OSError:
            continue
        rows.append(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}")
    if not rows:
        return ""
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def allow_tree_change(
    root: str | Path,
    *,
    state_path: str | Path,
    now: float | None = None,
    daily_refresh: bool = True,
) -> tuple[bool, str]:
    """Allow when a tree changed or when the local calendar day advanced."""
    path = Path(state_path)
    signature = tree_signature(root, exclude=(path.name,))
    if not signature:
        return False, "empty_tree"
    current = float(time.time() if now is None else now)
    local_day = datetime.fromtimestamp(current).strftime("%Y-%m-%d")
    state: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    except (OSError, TypeError, ValueError):
        state = {}
    unchanged = state.get("signature") == signature
    same_day = state.get("local_day") == local_day
    if unchanged and (same_day or not daily_refresh):
        return False, "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "signature": signature,
                "local_day": local_day,
                "checked_epoch": int(current),
            },
            sort_keys=True,
        ),
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True, "daily_refresh" if unchanged else "changed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    tree = sub.add_parser("tree")
    tree.add_argument("--root", required=True)
    tree.add_argument("--state", required=True)
    tree.add_argument("--now", type=float)
    args = parser.parse_args(argv)
    allowed, _reason = allow_tree_change(
        args.root, state_path=args.state, now=args.now
    )
    print("allow" if allowed else "skip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
