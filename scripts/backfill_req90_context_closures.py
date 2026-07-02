#!/usr/bin/env python3
"""One-shot backfill for REQ-90①: close the historical context-closure black holes.

int_879cb1472b / int_d9aa5c5668 are executed context-category MOMENTs whose
policy has followup=None — the pre-REQ-90 _on_moment_terminal wrote NOTHING on
that path, so closure_status stayed 'none' forever with no closed_at (the
exact violation of the "CAPTURE is ALWAYS maintained" contract). Live code now
writes na + closed_at at terminal time; this script applies the same UPDATE to
the rows that predate the fix, additionally guarded by
closure_status NOT IN ('done','recorded','na') so re-runs are no-ops.

Default is a DRY RUN (prints what would change, writes nothing).

    python3 scripts/backfill_req90_context_closures.py             # inspect
    python3 scripts/backfill_req90_context_closures.py --apply     # write
    python3 scripts/backfill_req90_context_closures.py --db /path/to/jarvis.db
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULT_DB = ROOT / "data" / "jarvis.db"

# The two black-hole rows confirmed in the 2026-07-02 v4 batch-2/3 plan.
TARGET_IDS = ["int_879cb1472b", "int_d9aa5c5668"]

_CLOSURE_TERMINAL = ("done", "recorded", "na")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help=f"path to jarvis.db (default: {DEFAULT_DB})")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is dry-run)")
    ap.add_argument("--ids", nargs="*", default=TARGET_IDS,
                    help=f"intent ids to backfill (default: {TARGET_IDS})")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[backfill-req90] DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[backfill-req90] {mode} on {db_path}")

    changed = 0
    for iid in args.ids:
        row = conn.execute(
            "SELECT id, name, status, category, closure_status, closure_result, "
            "closed_at FROM intentions WHERE id = ?", (iid,)).fetchone()
        if not row:
            print(f"  {iid}: NOT FOUND — skip")
            continue
        state = (f"status={row['status']} category={row['category']} "
                 f"closure_status={row['closure_status']} closed_at={row['closed_at']}")
        if row["closure_status"] in _CLOSURE_TERMINAL:
            print(f"  {iid}: already terminal ({state}) — no-op")
            continue
        print(f"  {iid}: {state} → closure_status=na closed_at={now}")
        if args.apply:
            cur = conn.execute(
                "UPDATE intentions SET closure_status = 'na', closed_at = ?, "
                "closure_result = ? WHERE id = ? "
                "AND closure_status NOT IN ('done','recorded','na')",
                (now, f"no-followup policy (category={row['category']}) "
                      f"[backfill REQ-90]", iid),
            )
            changed += cur.rowcount
        else:
            changed += 1

    if args.apply:
        conn.commit()
        print(f"[backfill-req90] updated {changed} row(s)")
    else:
        print(f"[backfill-req90] would update {changed} row(s) — "
              f"re-run with --apply to write")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
