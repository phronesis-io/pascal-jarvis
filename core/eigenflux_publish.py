"""Lifecycle helpers for Jarvis-authored EigenFlux broadcast drafts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.jsonl import read_jsonl
from core.timeutil import now_local_str

DRAFT_MAX_AGE_S = 48 * 3600
LAPSE_REASON = "广播草稿 48 小时未批，已自动归档"


def _draft_id(path: Path, data: dict) -> str:
    value = str(data.get("id") or "").strip()
    return value or path.stem


def _load_draft(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _lapse_matching_memorial(
    jarvis_dir: Path,
    *,
    pending_id: str,
    memorial_id: str = "",
) -> bool:
    """Close the approval card that belongs to an expired draft.

    Older drafts predate the explicit ``memorial_id`` field, so context is the
    compatibility key. Reading and writing the caller's root keeps tests and
    secondary installs isolated from the live ledger.
    """
    from core import memorial

    ledger = jarvis_dir / "memorials.jsonl"
    states = memorial._fold(read_jsonl(ledger))
    target = str(memorial_id or "").strip()
    if not target:
        marker = f"pending_publish id={pending_id}"
        for state in states.values():
            if (
                state.get("source") == "eigenflux-publish"
                and marker in str(state.get("context") or "")
            ):
                target = str(state.get("id") or "")
                break
    state = states.get(target)
    if not state or state.get("status") != "pending":
        return False
    memorial._append_line(
        ledger,
        {
            "ev": "lapse",
            "id": target,
            "ts": now_local_str(),
            "reason": LAPSE_REASON,
        },
    )
    return True


def reconcile_pending_drafts(
    jarvis_dir: str | Path,
    *,
    now: float | None = None,
    max_age_s: int = DRAFT_MAX_AGE_S,
) -> dict:
    """Archive stale drafts and converge their approval cards.

    Returns counts for the scheduler and UI. Existing files in ``expired/``
    are also reconciled, repairing cards produced before the lifecycle link
    was added.
    """
    root = Path(jarvis_dir)
    pending_dir = root / "eigenflux" / "pending_publish"
    expired_dir = pending_dir / "expired"
    current_time = time.time() if now is None else float(now)
    active = 0
    expired = 0
    lapsed = 0

    for path in sorted(pending_dir.glob("*.json")):
        try:
            age = current_time - path.stat().st_mtime
        except OSError:
            continue
        if age <= max_age_s:
            active += 1
            continue
        data = _load_draft(path)
        expired_dir.mkdir(parents=True, exist_ok=True)
        destination = expired_dir / path.name
        try:
            os.replace(path, destination)
        except OSError:
            continue
        expired += 1
        if _lapse_matching_memorial(
            root,
            pending_id=_draft_id(destination, data),
            memorial_id=str(data.get("memorial_id") or ""),
        ):
            lapsed += 1

    # Repair legacy drafts that were already moved before memorial linkage.
    for path in sorted(expired_dir.glob("*.json")):
        data = _load_draft(path)
        if _lapse_matching_memorial(
            root,
            pending_id=_draft_id(path, data),
            memorial_id=str(data.get("memorial_id") or ""),
        ):
            lapsed += 1

    return {"active": active, "expired": expired, "lapsed": lapsed}
