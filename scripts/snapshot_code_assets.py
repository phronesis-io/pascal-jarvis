#!/usr/bin/env python3
"""Private backup of Git history plus every uncommitted code asset.

Each repository gets a ``git bundle --all``. Every worktree also gets a binary
diff and copies of non-ignored untracked files, so a local draft or unfinished
agent change is recoverable without publishing it to a remote.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, check=check,
    )


def _safe_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:10]
    return f"{path.name}-{digest}"


def discover(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for parent_name in ("repos", "worktrees"):
        parent = root / parent_name
        if not parent.is_dir():
            continue
        candidates.extend(p for p in parent.iterdir() if p.is_dir())
    if (root / ".git").exists():
        candidates.append(root)
    found: dict[str, Path] = {}
    for candidate in candidates:
        result = _git(candidate, "rev-parse", "--show-toplevel", check=False)
        if result.returncode == 0:
            top = Path(result.stdout.decode().strip()).resolve()
            found[str(top)] = top
    return sorted(found.values(), key=str)


def snapshot(root: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    bundles = destination / "bundles"
    worktrees = destination / "worktrees"
    bundles.mkdir(exist_ok=True)
    worktrees.mkdir(exist_ok=True)
    records = []
    failures: list[str] = []
    repos = discover(root)
    groups: dict[str, dict] = {}

    # Build one complete bundle per common repository. ``--all`` protects
    # every ref, while explicit worktree HEADs also preserve detached commits
    # that may be the only base for a dirty patch.
    for repo in repos:
        common = _git(repo, "rev-parse", "--git-common-dir", check=False)
        head = _git(repo, "rev-parse", "HEAD", check=False)
        if common.returncode != 0:
            failures.append(f"common dir lookup failed for {repo}: exit {common.returncode}")
            continue
        raw = Path(common.stdout.decode().strip())
        common_path = str((repo / raw).resolve() if not raw.is_absolute() else raw.resolve())
        group = groups.setdefault(common_path, {"repo": repo, "heads": set()})
        if head.returncode == 0:
            group["heads"].add(head.stdout.decode().strip())

    bundle_for_common: dict[str, str] = {}
    for common_path, group in groups.items():
        repo = group["repo"]
        bundle_name = f"{_safe_name(repo)}.bundle"
        result = _git(
            repo, "bundle", "create", str(bundles / bundle_name), "--all",
            *sorted(group["heads"]), check=False,
        )
        if result.returncode != 0:
            failures.append(f"bundle failed for {repo}: exit {result.returncode}")
        else:
            bundle_for_common[common_path] = bundle_name

    for repo in repos:
        name = _safe_name(repo)
        target = worktrees / name
        target.mkdir(parents=True, exist_ok=True)
        head = _git(repo, "rev-parse", "HEAD", check=False)
        branch = _git(repo, "branch", "--show-current", check=False)
        common = _git(repo, "rev-parse", "--git-common-dir", check=False)
        common_path = ""
        bundle_name = ""
        if common.returncode == 0:
            raw = Path(common.stdout.decode().strip())
            common_path = str((repo / raw).resolve() if not raw.is_absolute() else raw.resolve())
            bundle_name = bundle_for_common.get(common_path, "")

        diff = _git(repo, "diff", "--binary", "HEAD", check=False)
        if diff.returncode == 0 and diff.stdout:
            (target / "working.patch").write_bytes(diff.stdout)
        elif diff.returncode != 0:
            failures.append(f"diff failed for {repo}: exit {diff.returncode}")

        untracked = _git(
            repo, "ls-files", "--others", "--exclude-standard", "-z",
            check=False,
        )
        copied = 0
        if untracked.returncode == 0:
            for raw_name in untracked.stdout.split(b"\0"):
                if not raw_name:
                    continue
                rel = Path(os.fsdecode(raw_name))
                if rel.is_absolute() or ".." in rel.parts:
                    failures.append(f"unsafe untracked path in {repo}: {rel}")
                    continue
                # Keep the lexical path so an untracked symlink is recognized
                # before resolution. Following it could copy an arbitrary
                # external secret into the snapshot under a misleading name.
                source = repo / rel
                if not source.is_file() and not source.is_symlink():
                    continue
                dest = target / "untracked" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if source.is_symlink():
                    # Store link metadata as inert text. Recreating a symlink
                    # that points outside the snapshot would evade checksums
                    # and could make a restore write outside its target.
                    dest = dest.with_name(dest.name + ".symlink")
                    dest.write_bytes(os.fsencode(os.readlink(source)))
                else:
                    shutil.copy2(source, dest)
                copied += 1
        else:
            failures.append(f"untracked scan failed for {repo}: exit {untracked.returncode}")

        status = _git(repo, "status", "--porcelain=v1", check=False)
        records.append({
            "path": str(repo),
            "head": head.stdout.decode().strip() if head.returncode == 0 else "",
            "branch": branch.stdout.decode().strip() if branch.returncode == 0 else "",
            "bundle": bundle_name,
            "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
            "untracked_files": copied,
            "working_patch": (target / "working.patch").exists(),
        })

    report = {"schema_version": 1, "repositories": records, "failures": failures}
    (destination / "assets.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    report = snapshot(Path(args.root).resolve(), Path(args.destination).resolve())
    print(f"[code-assets] {len(report['repositories'])} worktrees, "
          f"{len(report['failures'])} failures")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
