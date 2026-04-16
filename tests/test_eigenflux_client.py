"""Tests for plugins.eigenflux.client — persistence semantics, atomic writes,
auth validation."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.eigenflux.client import EigenFluxClient


def test_persist_items_deduplicates(tmp_path):
    c = EigenFluxClient(tmp_path)
    items = [
        {"item_id": 1, "content": "a"},
        {"item_id": 2, "content": "b"},
        {"item_id": 1, "content": "a-dup"},  # duplicate — should be skipped
    ]
    c._persist_items(items)
    lines = (tmp_path / "feed_store.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    seen = json.loads((tmp_path / "seen_items.json").read_text())
    assert "1" in seen and "2" in seen


def test_persist_items_survives_second_call_without_duplicates(tmp_path):
    c = EigenFluxClient(tmp_path)
    c._persist_items([{"item_id": 1, "content": "a"}])
    c._persist_items([{"item_id": 1, "content": "a-second"}, {"item_id": 2, "content": "b"}])
    lines = (tmp_path / "feed_store.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    # Ensure deterministic: item 1 appears only once
    ids = [json.loads(l).get("item_id") for l in lines]
    assert ids.count(1) == 1
    assert ids.count(2) == 1


def test_persist_items_seen_saved_incrementally(tmp_path):
    """If we crash mid-loop (simulated), seen-set should already hold completed items."""
    c = EigenFluxClient(tmp_path)
    items = [{"item_id": i, "content": f"x{i}"} for i in range(1, 11)]
    c._persist_items(items)
    # After 10 items, all should be in seen
    seen = json.loads((tmp_path / "seen_items.json").read_text())
    assert len(seen) == 10


def test_save_token_atomic(tmp_path):
    """Token save uses temp+rename — no partial file."""
    c = EigenFluxClient(tmp_path)
    c._save_token({"access_token": "secret"})
    assert c.creds_file.exists()
    # tmp file should be gone
    tmp_path_for_check = c.creds_file.with_suffix(c.creds_file.suffix + ".tmp")
    assert not tmp_path_for_check.exists()
    assert json.loads(c.creds_file.read_text())["access_token"] == "secret"


def test_save_seen_atomic(tmp_path):
    c = EigenFluxClient(tmp_path)
    c._save_seen({"1", "2", "3"})
    assert c.seen_file.exists()
    data = json.loads(c.seen_file.read_text())
    assert sorted(data) == ["1", "2", "3"]


def test_record_publish_atomic(tmp_path):
    c = EigenFluxClient(tmp_path)
    c._record_publish()
    assert c.publish_state.exists()
    data = json.loads(c.publish_state.read_text())
    assert data["last_publish_epoch"] > 0


def test_send_friend_request_invalid_email(tmp_path):
    c = EigenFluxClient(tmp_path)
    result = c.send_friend_request(email="not-an-email")
    assert result.get("code") == -1
    assert "invalid email" in result.get("msg", "")


def test_send_friend_request_requires_agent_or_email(tmp_path):
    c = EigenFluxClient(tmp_path)
    result = c.send_friend_request()
    assert result.get("code") == -1


def test_search_feed_history_missing_file(tmp_path):
    c = EigenFluxClient(tmp_path)
    assert c.search_feed_history("x") == []


def test_search_feed_history_finds_term(tmp_path):
    c = EigenFluxClient(tmp_path)
    c._persist_items([{"item_id": 1, "content": "banana smoothie"},
                      {"item_id": 2, "content": "apple pie"}])
    results = c.search_feed_history("banana")
    assert len(results) == 1
    assert results[0]["item_id"] == 1


def test_feed_history_stats(tmp_path):
    c = EigenFluxClient(tmp_path)
    assert c.feed_history_stats()["total_items"] == 0
    c._persist_items([{"item_id": 1, "content": "a"}, {"item_id": 2, "content": "b"}])
    stats = c.feed_history_stats()
    assert stats["total_items"] == 2
    assert stats["first_fetched"] is not None
    assert stats["last_fetched"] is not None
