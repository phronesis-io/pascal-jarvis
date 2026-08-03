"""Shared pytest fixtures + safety guards.

The session-autouse `_guard_repo_files` fixture checksums every critical
repo file before/after each test and FAILS the test if it was modified.
This prevents another regression where a test accidentally writes to
active_sessions.json (which happened before isolation was in place).
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make core/ and plugins/ importable from tests
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
_SUBPROCESS_RUN = subprocess.run

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
    ROOT / "engagement_log.jsonl",
    ROOT / "heartbeat_outbox.jsonl",
    ROOT / "memorials.jsonl",
    ROOT / "sched_events.jsonl",
    ROOT / "data" / "jarvis.db",
    # core.companion (2026-08-02). Added the same day the module landed: its
    # first test run wrote 51 rows of junk into the real ledger and stamped
    # data/companion_last_spoke — the exact file components.yaml now watches
    # for the silence alarm — because tests/test_checkin_post.py isolated
    # MEMORY_DIR but not JARVIS_DIR. The guard missed it only because these
    # paths were not listed here.
    ROOT / "data" / "companion_voice.jsonl",
    ROOT / "data" / "companion_last_spoke",
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
    ROOT / "engagement_log.jsonl",
    ROOT / "heartbeat_outbox.jsonl",
    ROOT / "memorials.jsonl",
    ROOT / "sched_events.jsonl",
    ROOT / "data" / "jarvis.db",
    ROOT / "data" / "companion_voice.jsonl",
    ROOT / "data" / "companion_last_spoke",
}


def _bot_is_running() -> bool:
    """True if the production heartbeat loop is live (so it may write runtime files)."""
    try:
        r = _SUBPROCESS_RUN(
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
def _isolate_runtime_database(monkeypatch, tmp_path):
    """Route every test's default SQLite access to a private database.

    Individual modules may still monkeypatch dashboard.db.DB_PATH when they
    need a named database.  The environment override covers all other stores
    that resolve JARVIS_DB_PATH directly and prevents CLI entry points from
    silently falling back to the live repo database.
    """
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "jarvis.db"))

    import dashboard.db as db_module
    import core.intentions as intentions

    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    intentions._table_ready = False


def _strict_guard() -> bool:
    """True when the live-bot exemption is disabled (CI-equivalent strictness)."""
    return str(os.environ.get("JARVIS_TEST_STRICT_GUARD") or "").strip() not in (
        "", "0", "false", "False")


# Mutations the live-bot exemption forgave, reported at the end of the run.
# Silence here is how a local "all passed" hid a red CI (2026-07-27, PR #12):
# the exemption is necessary on the production machine, but it must never be
# invisible — a forgiven write locally is a hard failure on CI.
_FORGIVEN: list[str] = []


@pytest.fixture(autouse=True)
def _guard_repo_files(_isolate_runtime_database, request):
    bot_was_live = _bot_is_running()
    before = {p: _checksum(p) for p in _PROTECTED_FILES}
    yield
    bot_live = None  # computed lazily, only if a mismatch shows up
    for p, old in before.items():
        new = _checksum(p)
        if old == new:
            continue
        # A live runtime file changed. If the production bot is running it owns
        # that write — not the test — so don't fail the suite for it, unless
        # strict mode asked for exactly the check CI performs.
        if p in _LIVE_RUNTIME_FILES and not _strict_guard():
            if bot_live is None:
                bot_live = bot_was_live or _bot_is_running()
            if bot_live:
                _FORGIVEN.append(f"{p.name}  ({request.node.nodeid})")
                continue
        raise AssertionError(
            f"PROTECTED FILE MODIFIED BY TEST: {p}\n"
            f"  before: {old}\n  after:  {new}\n"
            f"Check your fixtures — they must use tmp_path, not repo paths."
        )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Never let a locally-forgiven mutation read as a clean run."""
    if not _FORGIVEN:
        return
    terminalreporter.section("protected-file mutations forgiven", sep="!")
    terminalreporter.write_line(
        f"{len(_FORGIVEN)} write(s) to protected runtime files were forgiven "
        "because the production bot is live on this machine.")
    for entry in _FORGIVEN[:20]:
        terminalreporter.write_line(f"  - {entry}")
    if len(_FORGIVEN) > 20:
        terminalreporter.write_line(f"  ... and {len(_FORGIVEN) - 20} more")
    terminalreporter.write_line(
        "CI has no live bot and applies this check strictly, so these may be "
        "real failures there. Reproduce with JARVIS_TEST_STRICT_GUARD=1 "
        "(stop the bot first) before quoting this run as evidence.")


@pytest.fixture(autouse=True)
def _isolate_intent_telemetry(monkeypatch):
    """Intent lifecycle events must never reach the LIVE sched_events.jsonl.

    core.intentions._emit_intent hardcodes the repo root (production processes
    have no other home), so test intent operations — even against tmp
    databases — were emitting telemetry into the real event log: the 6/13
    verification found 688 test-fixture events ('约学妹', '读 x402'…)
    polluting the dashboard funnel. Tests that WANT to assert on telemetry
    re-patch this explicitly.
    """
    try:
        import core.intentions as _intents
        monkeypatch.setattr(_intents, "_emit_intent", lambda *a, **k: None)
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _isolate_daemon_log(monkeypatch, tmp_path):
    """Daemon log() must never reach the LIVE daemon.log.

    daemon.LOG_FILE binds at import time and nothing sets JARVIS_DAEMON_LOG
    under pytest, so it points at the real repo daemon.log. On 7/7 a
    deadletter test ran log() unstubbed and wrote fake WARN/ERROR rows into
    it (072cf2f stubbed that one fixture — per-fixture opt-in stays
    forgettable). Tests that assert on log output re-patch explicitly.
    """
    try:
        import daemon as _daemon
        monkeypatch.setattr(_daemon, "LOG_FILE", tmp_path / "daemon.log")
    except ImportError:
        pass
