#!/usr/bin/env python3
"""Retire EigenFlux skill files proven removed from the upstream tree."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def _previous_paths(repo: Path, previous_sha: str) -> set[str]:
    result = subprocess.run(
        [
            "git", "-C", str(repo), "ls-tree", "-r", "--name-only",
            f"{previous_sha}:skills",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise RuntimeError(
            "previous skill tree unavailable"
            + (f": {detail}" if detail else ""))
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    }


def _is_local_skill(path: Path) -> bool:
    try:
        return "jarvis-local" in (path / "SKILL.md").read_text(
            encoding="utf-8").lower()
    except OSError:
        return False


def retire_removed(
    upstream_repo: Path,
    previous_sha: str,
    source_skills: Path,
    destination_skills: Path,
) -> dict[str, list[str]]:
    """Remove only destination paths that the previous upstream tree owned."""
    previous = _previous_paths(upstream_repo, previous_sha)
    previous_skills = {path.split("/", 1)[0] for path in previous}
    removed_files: list[str] = []
    retired_skills: list[str] = []
    preserved_local_skills: set[str] = set()

    for skill in sorted(previous_skills):
        source_skill = source_skills / skill
        destination_skill = destination_skills / skill
        if source_skill.is_dir() or not destination_skill.is_dir():
            continue
        if _is_local_skill(destination_skill):
            preserved_local_skills.add(skill)
            continue
        destination_paths = {
            path.relative_to(destination_skills).as_posix()
            for path in destination_skill.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        # A removed upstream skill may have acquired Jarvis-only notes or
        # overlays since the previous sync. Preserve the directory in that
        # case; the per-file pass below removes only paths whose historical
        # upstream ownership is proven, leaving the unknown additions visible
        # to the caller's orphan review.
        if destination_paths - previous:
            continue
        shutil.rmtree(destination_skill)
        retired_skills.append(skill)

    for relative in sorted(previous):
        if relative.split("/", 1)[0] in preserved_local_skills:
            continue
        if (source_skills / relative).is_file():
            continue
        destination = destination_skills / relative
        if not destination.is_file() and not destination.is_symlink():
            continue
        destination.unlink()
        removed_files.append(relative)

    return {
        "removed_files": removed_files,
        "retired_skills": retired_skills,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-repo", required=True, type=Path)
    parser.add_argument("--previous-sha", required=True)
    parser.add_argument("--source-skills", required=True, type=Path)
    parser.add_argument("--destination-skills", required=True, type=Path)
    args = parser.parse_args()

    result = retire_removed(
        args.upstream_repo,
        args.previous_sha,
        args.source_skills,
        args.destination_skills,
    )
    for path in result["removed_files"]:
        print(f"REMOVED_FILE\t{path}")
    for skill in result["retired_skills"]:
        print(f"RETIRED_SKILL\t{skill}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
