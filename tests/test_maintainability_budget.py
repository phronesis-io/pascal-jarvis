from pathlib import Path

import pytest

from scripts.maintainability_budget import audit_budgets, load_budgets


ROOT = Path(__file__).resolve().parent.parent


def test_core_debt_budget_is_not_exceeded():
    budgets = load_budgets(ROOT / "docs" / "maintainability_budget.json")
    result = audit_budgets(ROOT, budgets)

    assert result["ok"] is True, result["violations"]


def test_budget_detects_file_and_function_growth(tmp_path):
    path = tmp_path / "core" / "large.py"
    path.parent.mkdir()
    path.write_text(
        "def workflow():\n" + "    value = 1\n" * 5,
        encoding="utf-8",
    )
    budgets = {
        "core/large.py": {"max_lines": 5, "max_function_lines": 4},
    }

    result = audit_budgets(tmp_path, budgets)

    assert result["ok"] is False
    assert {item["metric"] for item in result["violations"]} == {
        "lines", "max_function_lines",
    }


def test_budget_fails_closed_for_missing_or_invalid_python(tmp_path):
    missing = audit_budgets(
        tmp_path, {"core/missing.py": {"max_lines": 1, "max_function_lines": 1}},
    )
    assert missing["ok"] is False
    assert missing["violations"][0]["metric"] == "missing"

    broken = tmp_path / "broken.py"
    broken.write_text("def nope(:\n", encoding="utf-8")
    invalid = audit_budgets(
        tmp_path, {"broken.py": {"max_lines": 10, "max_function_lines": 10}},
    )
    assert invalid["ok"] is False
    assert invalid["violations"][0]["metric"] == "parse_error"


def test_budget_loader_rejects_incomplete_limits(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text('{"core/large.py":{"max_lines":10}}', encoding="utf-8")

    with pytest.raises(ValueError, match="max_function_lines"):
        load_budgets(path)
