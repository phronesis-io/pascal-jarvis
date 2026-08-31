"""Audit the Memorial ledger against the owner-interruption contract."""

from __future__ import annotations

import argparse
import json
import time

from core.interruption import audit
from core.memorial import list_memorials


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending", action="store_true")
    parser.add_argument("--days", type=float, default=0,
                        help="only include Items created in the last N days")
    args = parser.parse_args(argv)
    rows = list_memorials(pending_only=args.pending)
    if args.days > 0:
        cutoff = time.time() - args.days * 86400
        rows = [row for row in rows if float(row.get("epoch") or 0) >= cutoff]
    report = audit(rows)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["explicit_invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
