#!/usr/bin/env python3
"""One-shot reconciliation (REQ-122): archive ghost cards nobody ever saw.

Background, 2026-08-11: the ledger counted ~106 张未闭环 while the escrow
docket自报「待批 14 件」. A large slice of that gap is ghosts from the
web-desk era — cards created and "routed" to the phone/web desk, a surface
measured dead (14d: 170 web cards, 3 reads; the web transport unconditionally
reported success). They were never decided, never lapsed, never delivered to
any surface a human actually looks at. They will never be answered because
they were never seen.

This script moves them to 留中 through the NORMAL event stream:
``memorial.lapse()`` appends a ``lapse`` event with
``reason="web_surface_retired_backfill"``. No historical line is rewritten,
so a card Pascal scrolls back to and taps still revives exactly like any
other 留中 row.

Ghost definition (matches ``memorial.delivery_accepted`` semantics):
  status == pending, AND delivery_status is not a live-channel acceptance
  (``memorial.ACCEPTED_DELIVERY_STATUSES`` minus the dead desk). Rows
  whose status is ``web_only`` / ``phone_ready`` are dead-surface rows by
  definition and qualify at any age; anything else (``not_sent``, ``failed``,
  …) must additionally be older than ``--min-age-hours`` (default 48) so a
  card whose emitter still owns its transport is never swept mid-flight.

Idempotent: a second run finds the rows lapsed and does nothing.
Dry-run by default (prints what would change, writes nothing):

    python3 scripts/backfill_ghost_lapse.py            # inspect
    python3 scripts/backfill_ghost_lapse.py --apply    # write lapse events
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import memorial  # noqa: E402
from core.timeutil import now_local  # noqa: E402

REASON = "web_surface_retired_backfill"

# The retired desk's statuses: no human ever saw these, whatever their age.
DEAD_SURFACE_STATUSES = {"web_only", "phone_ready"}
# Acceptance by a channel that can actually reach a human (or, for
# ledger_only, a deliberate REQ-119 placement the morning digest covers).
# Derived from the module's own contract minus the dead desk — a hand-copied
# set here would silently drift the moment memorial adds a status.
LIVE_STATUSES = memorial.ACCEPTED_DELIVERY_STATUSES - DEAD_SURFACE_STATUSES


def find_ghosts(states: list[dict], now: datetime,
                min_age_h: float) -> list[dict]:
    """Pure selection — no writes. See module docstring for the contract."""
    ghosts = []
    for st in states:
        if str(st.get("status", "")) != "pending":
            continue
        ds = str(st.get("delivery_status", ""))
        if ds in LIVE_STATUSES:
            continue
        if ds not in DEAD_SURFACE_STATUSES:
            age = memorial._age_hours(st, now)
            if age is None or age < min_age_h:
                continue
        ghosts.append(st)
    return ghosts


def run(now: datetime | None = None, apply: bool = False,
        min_age_h: float = 48.0) -> dict:
    now = now or now_local()
    ghosts = find_ghosts(memorial.list_memorials(), now, min_age_h)
    acted: list[dict] = []
    if apply:
        # memorial.lapse() re-reads folded state and refuses non-pending rows,
        # so a concurrent 批红 between scan and write is never overwritten.
        acted = [st for st in ghosts if memorial.lapse(st["id"], REASON)]
    # The audit trail counts what actually happened: rows LAPSED on an apply
    # run (a candidate a concurrent 批红 rescued must not be reported as
    # archived), candidates on a dry run (the preview is the product).
    audited = acted if apply else ghosts
    return {
        "candidates": len(ghosts),
        "lapsed": len(acted),
        "apply": apply,
        "by_source": dict(Counter(
            str(st.get("source", "?")) for st in audited)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually append lapse events (default: dry run)")
    ap.add_argument("--min-age-hours", type=float, default=48.0,
                    help="age floor for non-dead-surface rows (default 48)")
    args = ap.parse_args(argv)

    summary = run(apply=args.apply, min_age_h=args.min_age_hours)
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"[{mode}] ghost cards: {summary['candidates']} candidates, "
          f"{summary['lapsed']} lapsed (reason={REASON})")
    for source, n in sorted(summary["by_source"].items(), key=lambda kv: -kv[1]):
        print(f"  {source}: {n}")
    if not args.apply and summary["candidates"]:
        print("re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
