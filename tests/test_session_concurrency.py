"""Concurrency tests for core.session.SessionManager — fcntl lock prevents
tracker corruption under parallel callers."""

import json
import threading
from pathlib import Path

from core.session import SessionManager


def test_parallel_writes_do_not_corrupt_tracker(tmp_path):
    tracker = tmp_path / "tracker.json"
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    results = []
    errors = []

    def worker(key: str):
        try:
            sm = SessionManager(tracker, sdir)
            sid, _ = sm.get_session(key)
            results.append((key, sid))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(f"user-{i}",)) for i in range(30)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"exceptions: {errors}"
    # Tracker should be readable as JSON (not corrupted)
    data = json.loads(tracker.read_text())
    assert len(data) == 30
    # Each session_id should be unique
    sids = {v["session_id"] for v in data.values()}
    assert len(sids) == 30


def test_same_key_parallel_is_stable(tmp_path):
    """Calling get_session('same-key') from 20 threads yields exactly one session_id."""
    tracker = tmp_path / "tracker.json"
    sdir = tmp_path / "sessions"
    sdir.mkdir()

    ids = []
    def worker():
        sm = SessionManager(tracker, sdir)
        sid, _ = sm.get_session("shared")
        ids.append(sid)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()

    # All calls should resolve to the same session_id (counter stays at 1)
    assert len(set(ids)) == 1
    data = json.loads(tracker.read_text())
    assert data["shared"]["counter"] == 1
