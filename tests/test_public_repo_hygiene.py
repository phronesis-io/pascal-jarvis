"""Public repository hygiene guards.

These tests keep local configuration and credential-shaped files out of git.
They intentionally inspect only filenames and .gitignore rules, not secret
contents.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_TRACKED_FILES = {
    ".admin_token",
    ".env",
    "jarvis.yaml",
    "sources.yaml",
}

FORBIDDEN_TRACKED_PATTERNS = (
    ".env.*",
    "*.key",
    "*.p12",
    "*.pem",
    "*.pfx",
    "secrets/*",
    "secrets/**/*",
)

REQUIRED_GITIGNORE_PATTERNS = (
    ".admin_token",
    ".env",
    ".env.*",
    "*.key",
    "*.p12",
    "*.pem",
    "*.pfx",
    "jarvis.yaml",
    "secrets/",
    "sources.yaml",
)


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_sensitive_local_config_is_not_tracked():
    tracked = set(_tracked_files())

    assert not (tracked & FORBIDDEN_TRACKED_FILES)
    offenders = [
        path for path in sorted(tracked)
        if any(fnmatch.fnmatch(path, pattern) for pattern in FORBIDDEN_TRACKED_PATTERNS)
    ]
    assert offenders == []


def test_gitignore_keeps_common_secret_shapes_local():
    ignore_lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    for pattern in REQUIRED_GITIGNORE_PATTERNS:
        assert pattern in ignore_lines
