"""Tests for robust claude-binary resolution (core/claude_bin.py).

Pins the resolution order that severs the launchd-minimal-PATH dependency behind
the 2026-06-15 "Claude CLI not found" brain-death incident.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import claude_bin
from core.claude_bin import resolve_claude_bin


def test_configured_executable_wins(monkeypatch):
    # An explicit, valid config path takes precedence over PATH/which.
    monkeypatch.setattr(claude_bin.shutil, "which", lambda _n: "/usr/bin/claude")
    assert resolve_claude_bin("/bin/sh") == "/bin/sh"


def test_nonexistent_configured_falls_through_to_which(monkeypatch):
    monkeypatch.setattr(claude_bin.shutil, "which",
                        lambda _n: "/bin/sh")  # pretend which found a real exe
    assert resolve_claude_bin("/no/such/claude") == "/bin/sh"


def test_falls_back_to_local_bin_when_path_lacks_it(monkeypatch, tmp_path):
    # The incident: which() (this process's PATH) can't find claude, but the
    # native-installer copy in ~/.local/bin exists. expanduser fallback must win.
    fake_home = tmp_path
    local = fake_home / ".local" / "bin"
    local.mkdir(parents=True)
    claude = local / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)

    monkeypatch.setattr(claude_bin.shutil, "which", lambda _n: None)
    monkeypatch.setenv("HOME", str(fake_home))
    # os.path.expanduser reads HOME on POSIX.
    assert resolve_claude_bin() == str(claude)


def test_returns_bare_claude_when_nothing_resolves(monkeypatch, tmp_path):
    # Behavior unchanged when truly nothing is found: bare 'claude' so the
    # caller's existing FileNotFoundError handling still applies.
    monkeypatch.setattr(claude_bin.shutil, "which", lambda _n: None)
    monkeypatch.setenv("HOME", str(tmp_path))  # empty home ⇒ no fallbacks exist
    assert resolve_claude_bin() == "claude"


def test_non_executable_configured_is_ignored(monkeypatch, tmp_path):
    f = tmp_path / "claude"
    f.write_text("x")
    f.chmod(0o644)  # not executable
    monkeypatch.setattr(claude_bin.shutil, "which", lambda _n: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_claude_bin(str(f)) == "claude"
