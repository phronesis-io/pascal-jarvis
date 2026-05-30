"""Shared pytest fixtures + safety guards.

The session-autouse `_guard_repo_files` fixture checksums every critical
repo file before/after each test and FAILS the test if it was modified.
This prevents another regression where a test accidentally writes to
active_sessions.json (which happened before isolation was in place).
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

# Make core/ and plugins/ importable from tests
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Files that MUST NOT be touched by any test run. If a test mutates any of
# these, the guard below will fail and point at the culprit.
_PROTECTED_FILES = [
    ROOT / "active_sessions.json",
    ROOT / "heartbeat_state.json",
    ROOT / "jarvis.yaml",
    ROOT / "HEARTBEAT.md",
    ROOT / "eigenflux" / "credentials.json",
    ROOT / "eigenflux" / "feed_store.jsonl",
    ROOT / "eigenflux" / "seen_items.json",
]

# A subset of the protected files are *live runtime state* that the production
# bot (core.heartbeat_loop) rewrites on every cycle. On Pascal's machine the bot
# runs continuously, so a real cycle can write one of these inside a test's
# before/after window — a false positive that has nothing to do with the test.
# We only exempt these when an actual bot process is running; in a clean env
# (CI, no daemon) the strict equality check below still catches test-induced
# mutation.
_LIVE_RUNTIME_FILES = {
    ROOT / "active_sessions.json",
    ROOT / "heartbeat_state.json",
    ROOT / "eigenflux" / "feed_store.jsonl",
    ROOT / "eigenflux" / "seen_items.json",
}


def _bot_is_running() -> bool:
    """True if the production heartbeat loop is live (so it may write runtime files)."""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "core.heartbeat_loop"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _checksum(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.md5(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _guard_repo_files():
    before = {p: _checksum(p) for p in _PROTECTED_FILES}
    yield
    bot_live = None  # computed lazily, only if a mismatch shows up
    for p, old in before.items():
        new = _checksum(p)
        if old == new:
            continue
        # A live runtime file changed. If the production bot is running it owns
        # that write — not the test — so don't fail the suite for it.
        if p in _LIVE_RUNTIME_FILES:
            if bot_live is None:
                bot_live = _bot_is_running()
            if bot_live:
                continue
        raise AssertionError(
            f"PROTECTED FILE MODIFIED BY TEST: {p}\n"
            f"  before: {old}\n  after:  {new}\n"
            f"Check your fixtures — they must use tmp_path, not repo paths."
        )
