"""Shared pytest fixtures + safety guards.

The session-autouse `_guard_repo_files` fixture checksums every critical
repo file before/after each test and FAILS the test if it was modified.
This prevents another regression where a test accidentally writes to
active_sessions.json (which happened before isolation was in place).
"""

import hashlib
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


def _checksum(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.md5(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _guard_repo_files():
    before = {p: _checksum(p) for p in _PROTECTED_FILES}
    yield
    for p, old in before.items():
        new = _checksum(p)
        assert old == new, (
            f"PROTECTED FILE MODIFIED BY TEST: {p}\n"
            f"  before: {old}\n  after:  {new}\n"
            f"Check your fixtures — they must use tmp_path, not repo paths."
        )
