"""Contracts that keep the protected CI gate aligned with local validation."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_and_localtest_validate_the_same_shell_surfaces():
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8")
    localtest = (ROOT / "scripts" / "localtest.sh").read_text(
        encoding="utf-8")

    for required in (
        "bash -n bot.sh",
        "bash -n restart.sh",
        "find tasks scripts -type f -name '*.sh' -exec bash -n {} +",
        "find tasks/ -name '*.sh'",
        "find scripts/ -name '*.sh'",
    ):
        assert required in workflow

    assert "find tasks scripts -type f -name '*.sh'" in localtest
    assert "find tasks/ -name '*.sh'" in localtest
    assert "find scripts/ -name '*.sh'" in localtest


def test_ci_keeps_parallel_coverage_data_outside_the_runtime_tree():
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8")

    assert "COVERAGE_FILE: ${{ runner.temp }}/jarvis-coverage" in workflow
    assert workflow.index("python -m coverage run") < workflow.index(
        "python -m coverage combine")


def test_ci_covers_production_python_and_compatibility_without_renaming_gate():
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8")

    assert "test:\n    runs-on:" in workflow
    assert 'python-version: "3.14"' in workflow
    assert "python-312-compat:" in workflow
    assert 'python-version: "3.12"' in workflow
    assert workflow.count("/usr/share/zoneinfo/Asia/Shanghai") == 2


def test_local_full_gate_runs_the_same_coverage_budget_as_ci():
    localtest = (ROOT / "scripts" / "localtest.sh").read_text(
        encoding="utf-8")

    assert "python3 -m coverage run -m pytest tests/" in localtest
    assert "python3 -m coverage combine" in localtest
    assert "python3 scripts/coverage_budget.py" in localtest
