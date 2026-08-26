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
