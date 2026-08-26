"""Tests for the branch/statement coverage ratchet."""

from scripts.coverage_budget import MINIMUMS, evaluate


def _summary(statements: float, branches: float) -> dict:
    return {
        "percent_statements_covered": statements,
        "percent_branches_covered": branches,
    }


def _passing_report() -> dict:
    total = _summary(*MINIMUMS["__total__"])
    files = {
        name: {"summary": _summary(*minimum)}
        for name, minimum in MINIMUMS.items()
        if name != "__total__"
    }
    return {"totals": total, "files": files}


def test_coverage_budget_accepts_every_reviewed_minimum():
    rows, violations = evaluate(_passing_report())
    assert len(rows) == len(MINIMUMS)
    assert violations == []


def test_coverage_budget_reports_missing_and_regressed_rows():
    report = _passing_report()
    report["files"].pop("core/matter_executor.py")
    report["files"]["core/heartbeat.py"]["summary"] = _summary(87.9, 87.8)

    _, violations = evaluate(report)

    assert "missing coverage row: core/matter_executor.py" in violations
    assert any("core/heartbeat.py statement coverage" in row
               for row in violations)
    assert any("core/heartbeat.py branch coverage" in row
               for row in violations)
