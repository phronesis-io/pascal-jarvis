"""Bound EigenFlux drafting to materially new local evidence.

Only a digest and the attempt time are persisted.  The candidate material can
contain private memory summaries, so it must never be copied into state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from core.safety import atomic_write


DEFAULT_RETRY_SECONDS = 24 * 3600


@dataclass(frozen=True)
class MaterialDecision:
    allowed: bool
    reason: str
    digest: str = ""


def _normalized(material: str) -> str:
    return "\n".join(
        line.rstrip() for line in material.splitlines() if line.strip()
    ).strip()


def material_digest(material: str) -> str:
    normalized = _normalized(material)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def gate_material(
    material: str,
    *,
    state_path: str | Path,
    now: float | None = None,
    retry_seconds: int = DEFAULT_RETRY_SECONDS,
) -> MaterialDecision:
    """Record and allow new material, or a bounded retry of unchanged material."""
    digest = material_digest(material)
    if not digest:
        return MaterialDecision(False, "no_material")

    path = Path(state_path)
    current = float(time.time() if now is None else now)
    state: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    except (OSError, ValueError, TypeError):
        state = {}

    previous_digest = str(state.get("last_attempt_digest", ""))
    try:
        previous_epoch = float(state.get("last_attempt_epoch", 0) or 0)
    except (TypeError, ValueError):
        previous_epoch = 0
    retry_seconds = max(60, int(retry_seconds))
    if digest == previous_digest and current - previous_epoch < retry_seconds:
        return MaterialDecision(False, "unchanged_material", digest)

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        json.dumps(
            {
                "schema_version": 1,
                "last_attempt_digest": digest,
                "last_attempt_epoch": int(current),
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    reason = "new_material" if digest != previous_digest else "retry_window_elapsed"
    return MaterialDecision(True, reason, digest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--retry-seconds", type=int, default=DEFAULT_RETRY_SECONDS)
    parser.add_argument("--now", type=float)
    args = parser.parse_args(argv)
    decision = gate_material(
        sys.stdin.read(),
        state_path=args.state,
        now=args.now,
        retry_seconds=args.retry_seconds,
    )
    print("allow" if decision.allowed else "skip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
