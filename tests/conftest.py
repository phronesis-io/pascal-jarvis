"""Shared pytest fixtures + safety guards.

The session-autouse `_guard_repo_files` fixture checksums every critical
repo file before/after each test and FAILS the test if it was modified.
This prevents another regression where a test accidentally writes to
active_sessions.json (which happened before isolation was in place).
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Make core/ and plugins/ importable from tests
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
_SUBPROCESS_RUN = subprocess.run

# Set HOME before pytest imports test modules. Several runtime modules resolve
# Path.home() into module-level constants during collection; a per-test fixture
# would be too late and could bind those constants to the operator's real
# ~/.jarvis, ~/.eigenflux, ~/.claude, or ~/.lark-cli trees.
_ORIGINAL_HOME = os.environ.get("HOME")
_ORIGINAL_EIGENFLUX_HOME = os.environ.get("EIGENFLUX_HOME")
_TEST_HOME = Path(tempfile.mkdtemp(prefix="jarvis-pytest-home-"))
for _relative in (".jarvis/memory", ".eigenflux", ".claude", ".codex", ".lark-cli"):
    (_TEST_HOME / _relative).mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(_TEST_HOME)
os.environ["EIGENFLUX_HOME"] = str(_TEST_HOME / ".eigenflux")


def pytest_unconfigure(config):
    if _ORIGINAL_HOME is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _ORIGINAL_HOME
    if _ORIGINAL_EIGENFLUX_HOME is None:
        os.environ.pop("EIGENFLUX_HOME", None)
    else:
        os.environ["EIGENFLUX_HOME"] = _ORIGINAL_EIGENFLUX_HOME
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def _propagate_coverage_to_python_subprocesses(monkeypatch):
    """Keep coverage active when a test intentionally supplies a tiny env.

    Task-hook tests run the production scripts in child interpreters and often
    replace the whole environment to prevent credential or runtime leakage.
    Coverage's subprocess patch works for inherited environments; this fixture
    copies only its instrumentation variables into explicit test envs. It is a
    no-op during ordinary pytest runs.
    """
    if not os.environ.get("COVERAGE_PROCESS_CONFIG"):
        yield
        return

    original_popen = subprocess.Popen
    keys = (
        "COVERAGE_FILE",
        "COVERAGE_PROCESS_CONFIG",
        "COVERAGE_PROCESS_START",
        "PYTHONPATH",
    )

    class CoveredPopen(original_popen):
        """Popen-compatible coverage injector, including its type contract.

        A function wrapper breaks dependencies that evaluate annotations such
        as ``subprocess.Popen[bytes]`` while the fixture is active on Python
        3.12. Subclassing keeps Popen callable, usable in isinstance checks,
        and subscriptable while still extending only explicit child envs.
        """

        def __init__(self, *args, **kwargs):
            if kwargs.get("env") is not None:
                child_env = dict(kwargs["env"])
                for key in keys:
                    value = os.environ.get(key)
                    if value:
                        child_env[key] = value
                kwargs["env"] = child_env
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", CoveredPopen)
    yield


# (The old autouse `_desk_reachable_pinned` fixture is gone with REQ-119:
# routing no longer consults the phone/web desk's live pairing state — Lark
# is the only delivery surface — so there is nothing to pin for hermeticity.)

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
    ROOT / "data" / ".daily_plan_stamp",
    ROOT / "data" / ".weekly_review_stamp",
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
    ROOT / "data" / ".daily_plan_stamp",
    ROOT / "data" / ".weekly_review_stamp",
}


def _bot_is_running() -> bool:
    """True if the production heartbeat loop is live.

    macOS ``pgrep -f`` truncates long Homebrew-Python command lines before
    their trailing ``-m core.heartbeat_loop``. Scan complete process args,
    matching the same token sequence bot.sh uses, so the write guard does not
    blame tests for files changed by a live production process.
    """
    try:
        r = _SUBPROCESS_RUN(
            ["ps", "-eo", "args"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0 and "-m core.heartbeat_loop" in (r.stdout or "")
    except Exception:
        return False


def _checksum(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.md5(path.read_bytes()).hexdigest()


def _metadata_snapshot() -> dict[Path, tuple[int, int, int]]:
    """Cheaply detect writes outside the historical protected-file list.

    Hashing the whole 49 MB runtime tree before every test would make the
    suite unusable.  Metadata is enough to catch newly-created files and the
    normal write/replace patterns used by Jarvis; the high-risk named files
    above still receive content hashes.  We watch every top-level file plus
    every entry under data/ and views/ so a new sidecar cannot silently fall
    outside this guard again.
    """
    paths: set[Path] = set()
    try:
        paths.update(
            entry for entry in ROOT.iterdir()
            if entry.is_file() and entry.name != ".git"
        )
    except OSError:
        pass
    for directory in (
        ROOT / "data",
        ROOT / "views",
        _TEST_HOME / ".jarvis",
        _TEST_HOME / ".eigenflux",
        _TEST_HOME / ".lark-cli",
        _TEST_HOME / ".claude",
        _TEST_HOME / ".codex",
    ):
        if not directory.exists():
            continue
        paths.add(directory)
        try:
            paths.update(directory.rglob("*"))
        except OSError:
            pass
    snapshot = {}
    for path in paths:
        if path in _PROTECTED_FILES:
            continue
        try:
            stat = path.stat()
            snapshot[path] = (
                int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_ino))
        except OSError:
            continue
    return snapshot


@pytest.fixture(autouse=True)
def _block_unmocked_lark_cli(monkeypatch, tmp_path_factory, request):
    """Fail a test that reaches the machine's real lark-cli executable."""
    import core.memorial as memorial

    monkeypatch.setattr(
        memorial,
        "_LARK_CARD_SYNC_RUNNER",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="{}",
            stderr="",
        ),
    )
    bin_dir = tmp_path_factory.mktemp("blocked-lark-cli")
    calls = bin_dir / "calls"
    shim = bin_dir / "lark-cli"
    shim.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$JARVIS_TEST_LARK_CALLS\"\n"
        "exit 97\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("JARVIS_TEST_LARK_CALLS", str(calls))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    yield
    if calls.exists() and calls.read_text(encoding="utf-8").strip():
        raise AssertionError(
            "UNMOCKED lark-cli CALL BY TEST: "
            f"{request.node.nodeid}\n{calls.read_text(encoding='utf-8')[:1000]}"
        )


@pytest.fixture(autouse=True)
def _isolate_runtime_database(monkeypatch, tmp_path):
    """Route every test's mutable runtime state to a private root.

    Individual modules may still monkeypatch core.db.DB_PATH when they
    need a named database. The environment overrides cover stores that resolve
    either JARVIS_DIR or JARVIS_DB_PATH and prevent CLI entry points from
    silently falling back to live repository state.
    """
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "jarvis.db"))

    import core.db as db_module
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
    # Strict is the default. A running production heartbeat is not proof that
    # it, rather than the current test, wrote a changed file. An explicit
    # opt-out remains available for diagnosis, but a normal green run can no
    # longer hide a real test leak behind an unrelated live process.
    return str(os.environ.get("JARVIS_TEST_STRICT_GUARD", "1")).strip() not in (
        "0", "false", "False")


# Mutations the live-bot exemption forgave, reported at the end of the run.
# Silence here is how a local "all passed" hid a red CI (2026-07-27, PR #12):
# the exemption is necessary on the production machine, but it must never be
# invisible — a forgiven write locally is a hard failure on CI.
_FORGIVEN: list[str] = []


@pytest.fixture(autouse=True)
def _guard_repo_files(_isolate_runtime_database, request):
    bot_was_live = _bot_is_running()
    before = {p: _checksum(p) for p in _PROTECTED_FILES}
    tree_before = _metadata_snapshot()
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
    tree_after = _metadata_snapshot()
    for p in sorted(set(tree_before) | set(tree_after), key=str):
        old = tree_before.get(p)
        new = tree_after.get(p)
        if old == new:
            continue
        if not _strict_guard():
            if bot_live is None:
                bot_live = bot_was_live or _bot_is_running()
            if bot_live:
                _FORGIVEN.append(f"{p.relative_to(ROOT)}  ({request.node.nodeid})")
                continue
        raise AssertionError(
            f"RUNTIME PATH MODIFIED BY TEST: {p}\n"
            f"  before: {old}\n  after:  {new}\n"
            "Inject JARVIS_DIR/root/tmp_path instead of writing into the repo."
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
        "This exemption was explicitly enabled with "
        "JARVIS_TEST_STRICT_GUARD=0. Reproduce in strict mode before quoting "
        "this run as evidence.")


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
