import subprocess
from pathlib import Path

import pytest

from tasks.eigenflux_preinstall_retire import retire_removed


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_retire_only_paths_proven_owned_by_previous_upstream(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    _write(upstream / "skills/ef-current/SKILL.md", "current")
    _write(upstream / "skills/ef-current/removed.md", "old")
    _write(upstream / "skills/ef-retired/SKILL.md", "retire")
    _write(upstream / "skills/ef-retired-local/SKILL.md", "retire carefully")
    _write(upstream / "skills/ef-local/SKILL.md", "once upstream")
    _git(upstream, "add", "skills")
    _git(upstream, "commit", "-m", "initial skills")
    previous = _git(upstream, "rev-parse", "HEAD")

    (upstream / "skills/ef-current/removed.md").unlink()
    for skill in ("ef-retired", "ef-retired-local", "ef-local"):
        (upstream / f"skills/{skill}/SKILL.md").unlink()
        (upstream / f"skills/{skill}").rmdir()
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-m", "retire skills")

    destination = tmp_path / "installed"
    _write(destination / "ef-current/SKILL.md", "current")
    _write(destination / "ef-current/removed.md", "old")
    _write(destination / "ef-current/local-note.md", "keep unknown")
    _write(destination / "ef-retired/SKILL.md", "retire")
    _write(destination / "ef-retired-local/SKILL.md", "retire carefully")
    _write(destination / "ef-retired-local/local-note.md", "keep unknown")
    _write(destination / "ef-local/SKILL.md", "metadata: jarvis-local")

    result = retire_removed(
        upstream,
        previous,
        upstream / "skills",
        destination,
    )

    assert result == {
        "removed_files": [
            "ef-current/removed.md",
            "ef-retired-local/SKILL.md",
        ],
        "retired_skills": ["ef-retired"],
    }
    assert not (destination / "ef-current/removed.md").exists()
    assert not (destination / "ef-retired").exists()
    assert not (destination / "ef-retired-local/SKILL.md").exists()
    assert (destination / "ef-retired-local/local-note.md").is_file()
    assert (destination / "ef-current/local-note.md").is_file()
    assert (destination / "ef-local/SKILL.md").is_file()


def test_bad_previous_sha_fails_closed_without_deleting(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init")
    destination = tmp_path / "installed"
    _write(destination / "ef-important/SKILL.md", "keep")

    with pytest.raises(RuntimeError, match="previous skill tree unavailable"):
        retire_removed(
            upstream,
            "not-a-commit",
            upstream / "skills",
            destination,
        )

    assert (destination / "ef-important/SKILL.md").is_file()


def test_dry_run_reports_same_retirement_without_mutating(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    _write(upstream / "skills/ef-current/SKILL.md", "current")
    _write(upstream / "skills/ef-current/removed.md", "old")
    _write(upstream / "skills/ef-retired/SKILL.md", "retire")
    _git(upstream, "add", "skills")
    _git(upstream, "commit", "-m", "initial skills")
    previous = _git(upstream, "rev-parse", "HEAD")

    (upstream / "skills/ef-current/removed.md").unlink()
    (upstream / "skills/ef-retired/SKILL.md").unlink()
    (upstream / "skills/ef-retired").rmdir()
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-m", "retire paths")

    destination = tmp_path / "installed"
    _write(destination / "ef-current/SKILL.md", "current")
    _write(destination / "ef-current/removed.md", "old")
    _write(destination / "ef-retired/SKILL.md", "retire")

    result = retire_removed(
        upstream,
        previous,
        upstream / "skills",
        destination,
        apply=False,
    )

    assert result == {
        "removed_files": ["ef-current/removed.md"],
        "retired_skills": ["ef-retired"],
    }
    assert (destination / "ef-current/removed.md").is_file()
    assert (destination / "ef-retired/SKILL.md").is_file()
