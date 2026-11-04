"""Opt-in private SDK tests; all identities, context and Feed are synthetic."""

from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time

import pytest

sdk = pytest.importorskip("eigenflux_sdk", reason="private binary wheel required")

from core import db as jarvis_db
from plugins.eigenflux.sdk_adapter import JarvisInbox, read_context


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(jarvis_db, "DB_PATH", tmp_path / "data" / "jarvis.db")
    monkeypatch.setattr(jarvis_db, "_connection", None)
    value = JarvisInbox(tmp_path)
    yield value
    if jarvis_db._connection is not None:
        jarvis_db._connection.close()


def delivery(**changes):
    values = dict(batch_id="synthetic-batch", sequence=1, agent_id="1",
                  feed={"items": [{"item_id": "100", "summary": "Synthetic robotics signal"}]},
                  deadline_at=int(time.time() * 1000) + 60000, _cancel=threading.Event())
    values.update(changes)
    return sdk.FeedDelivery(**values)


def test_duplicate_and_restart_accept_only_once(inbox, tmp_path):
    item = delivery()
    first = inbox.push_to_agent(item)
    assert first.status == "accepted"
    assert inbox.push_to_agent(item) == first
    assert JarvisInbox(tmp_path).delivery_status(item) == first
    assert inbox.snapshot()["accepted_batches"] == 1
    assert inbox.snapshot()["bookmarks"] == 1
    assert jarvis_db.bookmark_search("robotics")[0]["source"] == "eigenflux-sdk"


@pytest.mark.parametrize("changes", [{"agent_id": "2"}, {"sequence": 2}, {"feed": {"items": []}}])
def test_conflicting_batch_never_accepts(inbox, changes):
    assert inbox.push_to_agent(delivery()).status == "accepted"
    changed = delivery(**changes)
    assert inbox.push_to_agent(changed).status == "unknown"
    assert inbox.delivery_status(changed).status == "unknown"
    assert inbox.snapshot()["bookmarks"] == 1


def test_commit_response_loss_resolves_from_receipt(inbox, monkeypatch):
    original = jarvis_db.independent_connection

    @contextmanager
    def lost_response():
        with original() as db:
            yield db
        raise OSError("synthetic response loss after commit")

    with monkeypatch.context() as patch:
        patch.setattr(jarvis_db, "independent_connection", lost_response)
        assert inbox.push_to_agent(delivery()).status == "unknown"
    assert inbox.delivery_status(delivery()).status == "accepted"
    assert inbox.push_to_agent(delivery()).status == "accepted"
    assert inbox.snapshot()["bookmarks"] == 1


def test_cancelled_and_invalid_payload_leave_no_effect(inbox):
    item = delivery()
    item._cancel.set()
    assert inbox.push_to_agent(item).status == "busy"
    assert inbox.delivery_status(item).status == "not_accepted"
    broken = delivery(feed={"items": [{"summary": "missing identifier"}]})
    assert inbox.push_to_agent(broken).status == "unknown"
    assert inbox.snapshot()["accepted_batches"] == 0
    assert inbox.snapshot()["bookmarks"] == 0


def test_new_batch_deduplicates_same_item(inbox):
    assert inbox.push_to_agent(delivery()).status == "accepted"
    assert inbox.push_to_agent(delivery(batch_id="second", sequence=2)).status == "accepted"
    assert inbox.snapshot()["accepted_batches"] == 2
    assert inbox.snapshot()["bookmarks"] == 1


def test_context_explicit_forms_and_path_boundary(tmp_path):
    path = tmp_path / "context.json"
    for value in [{"summary": "Synthetic private context"}, {"fields": {"agent_description": "Synthetic Agent"}}]:
        path.write_text(json.dumps(value))
        assert read_context(path, tmp_path) == value
    for value in [{"summary": "x", "fields": {}}, {"summary": 1}, {"fields": []}, {"transcript": "x"}]:
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            read_context(path, tmp_path)
    with pytest.raises(ValueError):
        read_context(path, tmp_path / "elsewhere")
    path.write_text(" " * 16385)
    with pytest.raises(ValueError):
        read_context(path, tmp_path)


def test_database_must_match_explicit_root(tmp_path, monkeypatch):
    monkeypatch.setattr(jarvis_db, "DB_PATH", tmp_path / "other.db")
    with pytest.raises(ValueError):
        JarvisInbox(tmp_path)
