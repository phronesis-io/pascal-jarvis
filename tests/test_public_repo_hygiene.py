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

# ── Content guards (added 2026-07-13 pre-internal-release scrub) ──────────
# The 7/13 audit found real personal data (a private mailbox, full-length
# Lark IDs, financial figures) pasted into tracked files as "examples" or
# test fixtures. These guards catch the most identifiable shapes. Personal
# data belongs in gitignored locations (data/, jarvis.yaml, memory dirs).

import re

TEXT_SUFFIXES = {".py", ".sh", ".md", ".yaml", ".yml", ".json", ".txt", ".css", ".html"}

# Real consumer mailboxes are personal data; the only permitted addresses at
# these domains are synthetic fixtures, documented placeholders, the 163
# system sender, and public project addresses.
ALLOWED_ADDRESSES = {
    "user_1998@163.com",        # synthetic fixture (test_mail_triage)
    "u@163.com",                # synthetic fixture (test_perception)
    "you@163.com",              # placeholder (sources.example.yaml)
    "mailmaster@163.com",       # 163's own system sender
    "eigenfluxofficial@gmail.com",  # public project contact
}
CONSUMER_MAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@(?:163|126|qq|gmail|outlook|hotmail)\.[a-z]+")

# Full-length Lark open/chat ids identify a real tenant/user; docs must use
# truncated (…) or x-padded placeholders.
LARK_ID_RE = re.compile(r"\b(?:ou|oc|om)_[0-9a-f]{16,}\b")


def _tracked_text_files() -> list[Path]:
    return [ROOT / p for p in _tracked_files()
            if Path(p).suffix in TEXT_SUFFIXES]


def test_no_real_mailboxes_in_tracked_files():
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in CONSUMER_MAIL_RE.finditer(text):
            if m.group(0) not in ALLOWED_ADDRESSES:
                offenders.append(f"{path.relative_to(ROOT)}: {m.group(0)}")
    assert offenders == []


def test_no_full_length_lark_ids_in_tracked_files():
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in LARK_ID_RE.finditer(text):
            offenders.append(f"{path.relative_to(ROOT)}: {m.group(0)}")
    assert offenders == []
