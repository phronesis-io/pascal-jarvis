#!/usr/bin/env python3
"""Fail when runtime coverage falls below the reviewed baseline."""

from __future__ import annotations

import json
import sys
from pathlib import Path


# Budgets are a ratchet, not a quality target. Raise a row after adding tests;
# do not lower it to make a regression green.
MINIMUMS = {
    "__total__": (81.0, 73.0),
    "core/memorial.py": (89.0, 82.0),
    "core/intentions.py": (78.0, 72.0),
    "core/heartbeat.py": (88.0, 88.0),
    "core/delegations.py": (89.0, 75.0),
    "core/delivery.py": (81.0, 77.0),
    "core/heartbeat_loop.py": (75.0, 71.0),
    "core/ef_stream_loop.py": (72.0, 61.0),
    "core/lark_bot_transport.py": (83.0, 65.0),
    "core/matter_executor.py": (60.0, 46.0),
    "core/routine_evidence.py": (87.0, 75.0),
    "core/cross_session.py": (80.0, 41.0),
    "core/cross_session_index.py": (83.0, 70.0),
    "core/prompt.py": (81.0, 73.0),
}


def _percentages(summary: dict) -> tuple[float, float]:
    return (
        float(summary.get("percent_statements_covered", 0.0)),
        float(summary.get("percent_branches_covered", 0.0)),
    )


def evaluate(report: dict) -> tuple[list[str], list[str]]:
    """Return display rows and violations for one coverage JSON report."""
    files = report.get("files") or {}
    summaries = {"__total__": report.get("totals") or {}}
    summaries.update({name: row.get("summary") or {}
                      for name, row in files.items()})
    rows: list[str] = []
    violations: list[str] = []
    for name, (min_lines, min_branches) in MINIMUMS.items():
        summary = summaries.get(name)
        if summary is None:
            violations.append(f"missing coverage row: {name}")
            continue
        lines, branches = _percentages(summary)
        rows.append(
            f"{name}: statements={lines:.1f}% branches={branches:.1f}% "
            f"(minimum {min_lines:.1f}/{min_branches:.1f})"
        )
        if lines + 1e-9 < min_lines:
            violations.append(
                f"{name} statement coverage {lines:.1f}% < {min_lines:.1f}%")
        if branches + 1e-9 < min_branches:
            violations.append(
                f"{name} branch coverage {branches:.1f}% < {min_branches:.1f}%")
    return rows, violations


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print("usage: scripts/coverage_budget.py COVERAGE_JSON", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"coverage report unreadable: {exc}", file=sys.stderr)
        return 2
    rows, violations = evaluate(report)
    print("\n".join(rows))
    if violations:
        print("coverage budget failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("coverage budget passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
